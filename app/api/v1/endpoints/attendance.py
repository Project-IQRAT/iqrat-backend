from pydantic import BaseModel
from datetime import datetime, timedelta, date
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Body, Header, Request
from sqlalchemy.orm import Session
from sqlalchemy import func, and_
import math

from app.db.session import SessionLocal
from app.api.deps import get_db, get_current_user
from app.models.users import User, UserRole, Student, Lecturer, UserDevice, DeviceStatus
from app.models.academic import Timetable, SubjectOffering, ClassSession, Classroom, Subject, Section
from app.models.attendance import Attendance, AttendanceStatus, TokenStatus, DeviceLog
from app.core.security import get_password_hash # Using simple hash for token generation for now
import secrets

router = APIRouter()

# HELPER: Haversine Formula for Geofencing
def calculate_distance(lat1, lon1, lat2, lon2):
    R = 6371000  # Radius of Earth in meters
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2) * math.sin(dlambda/2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
    return R * c # Distance in meters

# ==========================================
# 1. LECTURER DASHBOARD (Classes for Today)
# ==========================================
@router.get("/lecturer/dashboard")
def get_todays_classes(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Returns classes scheduled for TODAY for this lecturer.
    """
    if current_user.role != UserRole.LECTURER:
        raise HTTPException(status_code=403, detail="Only lecturers can view this dashboard")

    # 1. Get Lecturer Profile
    lecturer = current_user.lecturer_profile
    if not lecturer:
        raise HTTPException(status_code=404, detail="Lecturer profile not found")

    # 2. Get Today's Day Name (e.g., "Monday")
    today_name = datetime.now().strftime("%A")
    
    # 3. Query Timetable
    # Join Timetable -> SubjectOffering -> Subject
    slots = db.query(Timetable).join(SubjectOffering).filter(
        SubjectOffering.lecturer_id == lecturer.id,
        Timetable.day_of_week == today_name
    ).all()

    # 4. Format for Frontend
    dashboard_data = []
    for slot in slots:
        dashboard_data.append({
            "timetable_id": slot.id,
            "subject_name": slot.offering.subject.name, # Access via relationships
            "subject_code": slot.offering.subject.code,
            "section": slot.section.name if slot.section else "N/A", # Assuming section linked
            "start_time": slot.start_time,
            "end_time": slot.end_time,
            "room": slot.classroom.room_no,
            "can_start": True # We can add logic to only enable button 10 mins before
        })
    
    return dashboard_data

# ==========================================
# 2. START CLASS (The "Start" Button) ▶️
# ==========================================
@router.post("/session/start")
def start_class_session(
    timetable_id: int = Body(..., embed=True),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    # 1. Verify Ownership
    lecturer = current_user.lecturer_profile
    slot = db.query(Timetable).join(SubjectOffering).filter(
        Timetable.id == timetable_id,
        SubjectOffering.lecturer_id == lecturer.id
    ).first()
    
    if not slot:
        raise HTTPException(status_code=404, detail="Class schedule not found or unauthorized")

    # 2. Check if already started today
    today = date.today()
    existing_session = db.query(ClassSession).filter(
        ClassSession.timetable_id == timetable_id,
        ClassSession.session_date == today
    ).first()
    
    if existing_session:
        # If already exists, just return it (Resume class)
        return {"msg": "Resuming Session", "session_id": existing_session.id}

    # 3. Create New Session
    new_session = ClassSession(
        timetable_id=timetable_id,
        session_date=today,
        status="ongoing"
    )
    db.add(new_session)
    db.commit()
    db.refresh(new_session)
    
    return {"msg": "Class Started", "session_id": new_session.id}

# ==========================================
# 3. GENERATE/REFRESH QR (The "10s Loop") 🔄
# ==========================================
@router.get("/session/qr/{session_id}")
def get_qr_token(
    session_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    # 1. Expire Old Tokens for this session
    db.query(QRToken).filter(
        QRToken.class_session_id == session_id,
        QRToken.status == TokenStatus.ACTIVE
    ).update({"status": TokenStatus.EXPIRED})
    
    # 2. Generate New Token (Random Secure String)
    token_str = secrets.token_urlsafe(16) # e.g., "Xu28_s9d..."
    
    # 3. Save
    new_token = QRToken(
        class_session_id=session_id,
        token_value=token_str,
        expires_at=datetime.now() + timedelta(seconds=12), # 10s + 2s buffer
        status=TokenStatus.ACTIVE
    )
    db.add(new_token)
    db.commit()
    
    return {"qr_token": token_str, "expires_in": 10}

# ==========================================
# 4. SCAN QR (The "Student Action") 📱🛡️
# ==========================================
class ScanRequest(BaseModel):
    # We define what the phone sends us
    pass 

@router.post("/scan")
def scan_qr(
    token: str = Body(...),
    latitude: float = Body(...),
    longitude: float = Body(...),
    device_fingerprint: str = Body(...), # Unique ID from phone (e.g., UUID)
    device_name: str = Body("Unknown Device"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if current_user.role != UserRole.STUDENT:
        raise HTTPException(status_code=403, detail="Only students can scan")

    student = current_user.student_profile

    # --- CHECK 1: Token Validity ---
    qr_record = db.query(QRToken).filter(QRToken.token_value == token).first()
    
    if not qr_record:
        raise HTTPException(status_code=404, detail="Invalid QR Code")
        
    if qr_record.status != TokenStatus.ACTIVE or qr_record.expires_at < datetime.now():
        raise HTTPException(status_code=400, detail="QR Code Expired. Please scan again.")

    session = qr_record.class_session

    # --- CHECK 2: Duplicate Attendance ---
    existing_attendance = db.query(Attendance).filter(
        Attendance.student_id == student.id,
        Attendance.class_session_id == session.id
    ).first()
    
    if existing_attendance:
        return {"msg": "Attendance already marked", "status": existing_attendance.status}

    # --- CHECK 3: Geofencing (50m Radius) ---
    classroom = session.timetable.classroom
    if classroom.latitude and classroom.longitude:
        dist = calculate_distance(latitude, longitude, classroom.latitude, classroom.longitude)
        if dist > 50: # 50 Meters
            # We log the attempt but don't mark present
            # You might want to flag this as "Suspicious"
             raise HTTPException(status_code=400, detail=f"Location mismatch. You are {int(dist)}m away from class.")
    
    # --- CHECK 4: Device Fingerprinting (The 2-Device Rule) ---
    user_devices = db.query(UserDevice).filter(UserDevice.user_id == current_user.id).all()
    
    # Check if this fingerprint is already known
    known_device = next((d for d in user_devices if d.device_fingerprint == device_fingerprint), None)
    
    if not known_device:
        # It's a new device. Check limit.
        if len(user_devices) >= 2:
            # BLOCK! 🚫
            raise HTTPException(
                status_code=403, 
                detail="Device Limit Reached (Max 2). Request a reset from Admin."
            )
        else:
            # Register New Device
            new_device = UserDevice(
                user_id=current_user.id,
                device_fingerprint=device_fingerprint,
                device_name=device_name,
                status=DeviceStatus.ACTIVE
            )
            db.add(new_device)
            db.commit()

    # --- CHECK 5: Late vs Present (15 Min Rule) ---
    # Combine the session date and timetable start_time to get exact starting datetime
    class_start_dt = datetime.combine(session.session_date, session.timetable.start_time)
    
    # If current time is strictly greater than start_time + 15 mins
    if datetime.now() > class_start_dt + timedelta(minutes=15):
        attendance_status = AttendanceStatus.LATE
    else:
        attendance_status = AttendanceStatus.PRESENT
    
    # Create Attendance Record
    new_attendance = Attendance(
        student_id=student.id,
        class_session_id=session.id,
        status=attendance_status,
        scan_time=datetime.now(),
        verified_geo=True,
        location_lat=latitude,
        location_long=longitude
    )
    db.add(new_attendance)
    db.commit()

    db.add(DeviceLog(attendance_id=new_attendance.id, device_fingerprint=device_fingerprint))
    db.commit()

    return {"msg": "Attendance Marked Successfully", "status": attendance_status}


# ==========================================
# 5. LIVE STATS (The "Right Side" Feed) 📊
# ==========================================
@router.get("/session/live/{session_id}")
def get_live_attendance(
    session_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    # 1. Count Total Scanned
    count = db.query(Attendance).filter(Attendance.class_session_id == session_id).count()
    
    # 2. Get Recent Scans (List of names)
    recent_scans = db.query(Attendance).filter(Attendance.class_session_id == session_id)\
        .order_by(Attendance.scan_time.desc()).limit(10).all()
        
    student_list = []
    for att in recent_scans:
        student_list.append({
            "name": att.student.full_name, # Assuming student has full_name
            "reg_no": att.student.reg_no,
            "time": att.scan_time.strftime("%H:%M:%S")
        })
        
    return {
        "total_present": count,
        "recent_students": student_list
    }

# ==========================================
# 6. STOP CLASS SESSION (The "Kill Switch") 
# ==========================================
@router.post("/session/stop/{session_id}")
def stop_class_session(
    session_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if current_user.role != UserRole.LECTURER:
        raise HTTPException(status_code=403, detail="Only lecturers can stop sessions")

    session = db.query(ClassSession).filter(ClassSession.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    # Mark session as completed
    session.status = "completed"
    
    # Kill all active QR tokens for this session immediately
    db.query(QRToken).filter(
        QRToken.class_session_id == session_id, 
        QRToken.status == TokenStatus.ACTIVE
    ).update({"status": TokenStatus.EXPIRED})
    
    db.commit()
    return {"msg": "Session Stopped. No more scans allowed."}

# ==========================================
# 7. MANUAL OVERRIDE (For broken phones) 
# ==========================================
class ManualAttendance(BaseModel):
    student_id: int
    status: AttendanceStatus = AttendanceStatus.PRESENT
    remarks: str = "Manual override by lecturer"

@router.post("/session/{session_id}/manual")
def manual_attendance(
    session_id: int,
    data: ManualAttendance,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if current_user.role != UserRole.LECTURER:
        raise HTTPException(status_code=403, detail="Only lecturers can mark manual attendance")

    # Check if student already marked
    existing = db.query(Attendance).filter(
        Attendance.student_id == data.student_id,
        Attendance.class_session_id == session_id
    ).first()

    if existing:
        # Update existing
        existing.status = data.status
        existing.remarks = data.remarks
        db.commit()
        return {"msg": f"Updated student to {data.status}"}
    else:
        # Create new
        new_att = Attendance(
            student_id=data.student_id,
            class_session_id=session_id,
            status=data.status,
            remarks=data.remarks,
            verified_geo=False # Since it's manual, geo is false
        )
        db.add(new_att)
        db.commit()
        return {"msg": f"Marked student as {data.status} manually"}

# ==========================================
# 8. STUDENT DASHBOARD (History) 
# ==========================================
@router.get("/student/my-attendance")
def get_student_history(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if current_user.role != UserRole.STUDENT:
        raise HTTPException(status_code=403, detail="Only students can view this")

    student = current_user.student_profile
    
    # Get all attendance records for this student, ordered by newest
    records = db.query(Attendance).filter(Attendance.student_id == student.id)\
                .order_by(Attendance.scan_time.desc()).all()
                
    history = []
    for r in records:
        history.append({
            "subject": r.class_session.timetable.offering.subject.name,
            "date": r.class_session.session_date,
            "time": r.scan_time.strftime("%H:%M") if r.scan_time else "N/A",
            "status": r.status
        })
        
    return history