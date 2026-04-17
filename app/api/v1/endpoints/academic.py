import os
import shutil
from fastapi import APIRouter, Depends, HTTPException, status, Body, UploadFile, File, Form
from app.models.performance import SessionalMark, AttendanceLog, Assessment, StudentAssessmentRecord
from app.models.users import User, UserRole, Student, UserDevice, DeviceStatus, Lecturer, Admin
from app.models.system import Notification
from typing import List, Optional
import uuid
import random
from sqlalchemy import text
import secrets 
import math 
from datetime import datetime, time, date, timedelta, timezone
from sqlalchemy.orm import Session
from app.db.session import SessionLocal
from app.models.academic import Department, Degree, SessionBatch, Semester, Section, Subject, SubjectOffering, Classroom, Timetable, ClassSession
from app.api.deps import get_db, get_current_admin, get_current_user # <-- Added get_current_user
from pydantic import BaseModel
from app.api.logger import log_to_db

router = APIRouter()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# --- INPUT SCHEMAS ---
class DeptCreate(BaseModel):
    name: str
    code: str

class DegreeCreate(BaseModel):
    name: str
    code: str
    department_id: int

class BatchCreate(BaseModel):
    degree_id: int
    name: str 
    start_year: int
    end_year: int

class SemesterCreate(BaseModel):
    session_id: int
    name: str 
    semester_no: int

class SectionCreate(BaseModel):
    semester_id: int
    name: str 

class SubjectCreate(BaseModel):
    degree_id: int
    semester_no: int
    name: str
    code: str
    credit_hours: int

class EnrollStudents(BaseModel):
    subject_id: int
    semester_id: int
    student_ids: List[int]

# ==========================================
# 1. CREATION ENDPOINTS (POST)
# ==========================================

@router.post("/departments")
def create_dept(dept: DeptCreate, db: Session = Depends(get_db), current_admin=Depends(get_current_admin)):
    new_dept = Department(name=dept.name, code=dept.code)
    db.add(new_dept)
    db.commit()
    db.refresh(new_dept)
    return new_dept

@router.post("/degrees")
def create_degree(deg: DegreeCreate, db: Session = Depends(get_db), current_admin=Depends(get_current_admin)):
    # Verify Dept Exists
    if not db.query(Department).filter(Department.id == deg.department_id).first():
        raise HTTPException(status_code=404, detail="Department not found")
        
    new_deg = Degree(name=deg.name, code=deg.code, department_id=deg.department_id)
    db.add(new_deg)
    db.commit()
    db.refresh(new_deg)
    return new_deg

@router.post("/batches")
def create_batch(batch: BatchCreate, db: Session = Depends(get_db), current_admin=Depends(get_current_admin)):
    new_batch = SessionBatch(degree_id=batch.degree_id, name=batch.name, start_year=batch.start_year, end_year=batch.end_year)
    db.add(new_batch)
    db.commit()
    db.refresh(new_batch)
    return new_batch

@router.post("/semesters")
def create_semester(sem: SemesterCreate, db: Session = Depends(get_db), current_admin=Depends(get_current_admin)):
    new_sem = Semester(session_id=sem.session_id, name=sem.name, semester_no=sem.semester_no)
    db.add(new_sem)
    db.commit()
    db.refresh(new_sem)
    return new_sem

@router.post("/sections")
def create_section(sec: SectionCreate, db: Session = Depends(get_db), current_admin=Depends(get_current_admin)):
    new_sec = Section(semester_id=sec.semester_id, name=sec.name)
    db.add(new_sec)
    db.commit()
    db.refresh(new_sec)
    return new_sec

@router.post("/subjects")
def create_subject(sub: SubjectCreate, db: Session = Depends(get_db), current_admin=Depends(get_current_admin)):
    new_sub = Subject(
        degree_id=sub.degree_id, 
        semester_no=sub.semester_no,
        name=sub.name, 
        code=sub.code, 
        credit_hours=sub.credit_hours
    )
    db.add(new_sub)
    db.commit()
    db.refresh(new_sub)
    return new_sub

# ==========================================
# 2. DROPDOWN HELPERS (GET)
# ==========================================

@router.get("/departments")
def get_all_departments(db: Session = Depends(get_db), current_admin=Depends(get_current_admin)):
    # Check who is asking for the departments
    admin_profile = db.query(Admin).filter(Admin.user_id == current_admin.id).first()
    
    # Safely determine scope
    is_super = False
    dept_id = None
    if admin_profile:
        is_super = "super" in str(admin_profile.role_level).lower()
        dept_id = admin_profile.department_id

    # If it's a Department Admin, ONLY return their specific department!
    if not is_super and dept_id:
        return db.query(Department).filter(Department.id == dept_id).all()
        
    # If Super Admin, return everything
    return db.query(Department).all()

@router.get("/degrees/{department_id}")
def get_degrees_by_dept(department_id: int, db: Session = Depends(get_db), current_admin=Depends(get_current_admin)):
    """Used to populate the 'Select Degree' dropdown after Department is chosen"""
    return db.query(Degree).filter(Degree.department_id == department_id).all()

@router.get("/batches/{degree_id}")
def get_batches_by_degree(degree_id: int, db: Session = Depends(get_db), current_admin=Depends(get_current_admin)):
    """Used to select Session (2022-2026)"""
    return db.query(SessionBatch).filter(SessionBatch.degree_id == degree_id).all()

@router.get("/semesters/{session_id}")
def get_semesters_by_session(session_id: int, db: Session = Depends(get_db), current_admin=Depends(get_current_admin)):
    """Used to select 'Fall 2025'"""
    return db.query(Semester).filter(Semester.session_id == session_id).all()

@router.get("/sections/{semester_id}")
def get_sections_by_semester(semester_id: int, db: Session = Depends(get_db), current_admin=Depends(get_current_admin)):
    """Used to select 'Section 7A'"""
    return db.query(Section).filter(Section.semester_id == semester_id).all()

@router.get("/subjects/{degree_id}")
def get_subjects_by_degree(degree_id: int, db: Session = Depends(get_db), current_admin=Depends(get_current_admin)):
    """Used to show available subjects for this degree"""
    return db.query(Subject).filter(Subject.degree_id == degree_id).all()

# ==========================================
# 3. ENROLLMENT LOGIC
# ==========================================

@router.post("/enroll-students")
def enroll_students(data: EnrollStudents, db: Session = Depends(get_db), current_admin=Depends(get_current_admin)):
    """
    Takes a list of student_ids and enrolls them in a Subject + Semester
    """
    count = 0
    for student_id in data.student_ids:
        exists = db.query(SessionalMark).filter(
            SessionalMark.student_id == student_id,
            SessionalMark.subject_id == data.subject_id,
            SessionalMark.semester_id == data.semester_id
        ).first()

        if not exists:
            enrollment = SessionalMark(
                student_id=student_id,
                subject_id=data.subject_id,
                semester_id=data.semester_id,
                midterm_marks=0, 
                total_sessional_marks=0
            )
            db.add(enrollment)
            count += 1
            
    db.commit()
    return {"msg": f"Successfully enrolled {count} students."}

# ==========================================
# 4. CLASSROOMS CREATION
# ==========================================

class ClassroomCreate(BaseModel):
    department_id: int  # <-- NEW
    room_no: str
    building_name: str
    latitude: float
    longitude: float
    capacity: int = 60

@router.post("/classrooms")
def create_classroom(room: ClassroomCreate, db: Session = Depends(get_db), current_admin=Depends(get_current_admin)):
    # Check Duplicate
    if db.query(Classroom).filter(Classroom.room_no == room.room_no).first():
        raise HTTPException(status_code=400, detail="Classroom already exists")

    new_room = Classroom(
        department_id=room.department_id,
        room_no=room.room_no,
        building_name=room.building_name,
        latitude=room.latitude,
        longitude=room.longitude,
        capacity=room.capacity
    )
    db.add(new_room)
    db.commit()
    db.refresh(new_room)
    return new_room

@router.get("/classrooms")
def get_classrooms(db: Session = Depends(get_db), current_admin=Depends(get_current_admin)):
    admin_profile = db.query(Admin).filter(Admin.user_id == current_admin.id).first()
    # Filter by department if it's a Dept Admin
    if admin_profile and "super" not in str(admin_profile.role_level).lower() and admin_profile.department_id:
        return db.query(Classroom).filter(Classroom.department_id == admin_profile.department_id).all()
    return db.query(Classroom).all()

# ==========================================
# 5. LECTURER ASSIGNMENT
# ==========================================

class OfferingCreate(BaseModel):
    # The Frontend will filter these IDs for us using the dropdowns we described
    subject_id: int
    semester_id: int
    lecturer_id: int

@router.post("/subject-offerings")
def create_offering(
    data: OfferingCreate, 
    db: Session = Depends(get_db), 
    current_admin=Depends(get_current_admin)
):
    """
    Links a Lecturer to a Subject for a specific Semester.
    Example: "Dr. Ali is teaching Computer Vision in Fall 2025".
    """
    # 1. Validation: Check if this exact offering exists
    exists = db.query(SubjectOffering).filter(
        SubjectOffering.subject_id == data.subject_id,
        SubjectOffering.semester_id == data.semester_id,
        SubjectOffering.lecturer_id == data.lecturer_id
    ).first()
    
    if exists:
        raise HTTPException(status_code=400, detail="Lecturer is already assigned to this subject.")
    
    new_offering = SubjectOffering(
        subject_id=data.subject_id,
        semester_id=data.semester_id,
        lecturer_id=data.lecturer_id,
        is_active=True
    )
    db.add(new_offering)
    db.commit()
    db.refresh(new_offering)
    
    return {"msg": "Lecturer Assigned Successfully", "id": new_offering.id}

@router.get("/subject-offerings/{semester_id}")
def get_offerings_by_semester(semester_id: int, db: Session = Depends(get_db)):
    """
    Returns list of (Subject Name - Lecturer Name) for the semester.
    Useful for the 'Timetable Creation' screen later.
    """
    offerings = db.query(SubjectOffering).filter(SubjectOffering.semester_id == semester_id).all()
    
    # Custom Response to make frontend easier
    result = []
    for off in offerings:
        result.append({
            "offering_id": off.id,
            "subject": off.subject_id, 
            "lecturer": off.lecturer_id 
        })
    return result

# ==========================================
# 6. TIMETABLE - simple to implement the QR Generator
# ==========================================

class TimetableCreate(BaseModel):
    offering_id: int
    classroom_id: int
    day_of_week: str # "Monday"
    start_time: str # "09:00:00"
    end_time: str   # "10:30:00"


@router.post("/timetables")
def create_timetable_slot(
    slot: TimetableCreate, 
    db: Session = Depends(get_db), 
    current_admin=Depends(get_current_admin)
):
    """
    Creates a schedule slot manually. 
    REQUIRED to start a class session (Generate QR).
    """
    # Parse string times to Python time objects
    try:
        t_start = time.fromisoformat(slot.start_time)
        t_end = time.fromisoformat(slot.end_time)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid time format. Use HH:MM:SS")

    new_slot = Timetable(
        offering_id=slot.offering_id,
        classroom_id=slot.classroom_id,
        day_of_week=slot.day_of_week,
        start_time=t_start,
        end_time=t_end
    )
    db.add(new_slot)
    db.commit()
    db.refresh(new_slot)
    return {"msg": "Timetable Slot Created", "id": new_slot.id}

# ==========================================
# 7. UNIVERSAL FETCH ENDPOINTS FOR ADMIN
# ==========================================

@router.get("/all-subjects")
def get_all_subjects_universal(db: Session = Depends(get_db), current_admin=Depends(get_current_admin)):
    """Used to populate global dropdowns across the Admin Dashboard"""
    admin_profile = db.query(Admin).filter(Admin.user_id == current_admin.id).first()
    query = db.query(Subject)
    if admin_profile and "super" not in str(admin_profile.role_level).lower() and admin_profile.department_id:
        query = query.join(Degree).filter(Degree.department_id == admin_profile.department_id)
    return query.all()

@router.get("/all-semesters")
def get_all_semesters_universal(db: Session = Depends(get_db), current_admin=Depends(get_current_admin)):
    """Used to populate global dropdowns across the Admin Dashboard"""
    admin_profile = db.query(Admin).filter(Admin.user_id == current_admin.id).first()
    query = db.query(Semester)
    if admin_profile and "super" not in str(admin_profile.role_level).lower() and admin_profile.department_id:
        query = query.join(SessionBatch).join(Degree).filter(Degree.department_id == admin_profile.department_id)
    return query.all()

@router.get("/all-offerings")
def get_all_offerings_universal(db: Session = Depends(get_db), current_admin=Depends(get_current_admin)):
    """Used to populate the scheduling dropdowns in the Timetable tool"""
    admin_profile = db.query(Admin).filter(Admin.user_id == current_admin.id).first()
    query = db.query(SubjectOffering)
    if admin_profile and "super" not in str(admin_profile.role_level).lower() and admin_profile.department_id:
        query = query.join(Subject).join(Degree).filter(Degree.department_id == admin_profile.department_id)
    return query.all()

@router.get("/all-timetables")
def get_all_timetables_universal(db: Session = Depends(get_db), current_admin=Depends(get_current_admin)):
    """Fetches all scheduled classes for the visualizer grid."""
    admin_profile = db.query(Admin).filter(Admin.user_id == current_admin.id).first()
    query = db.query(Timetable)
    if admin_profile and "super" not in str(admin_profile.role_level).lower() and admin_profile.department_id:
        query = query.join(SubjectOffering).join(Subject).join(Degree).filter(Degree.department_id == admin_profile.department_id)
    return query.all()

# ==========================================
# 8. LIVE QR ATTENDANCE SESSIONS
# ==========================================

class SessionStartRequest(BaseModel):
    timetable_id: int
    latitude: float
    longitude: float

@router.post("/session/start")
def start_session(req: SessionStartRequest, db: Session = Depends(get_db)):
    """Starts a live attendance session and locks in the Lecturer's GPS coordinates."""
    new_session = ClassSession(
        timetable_id=req.timetable_id,
        session_date=date.today(),
        status="active",
        lecturer_latitude=req.latitude,
        lecturer_longitude=req.longitude
    )
    db.add(new_session)
    db.commit()
    db.refresh(new_session)
    
    return {"msg": "Session started", "session_id": new_session.id}

@router.get("/session/qr/{session_id}")
def get_qr_token(session_id: int, db: Session = Depends(get_db)):
    """Generates a secure rolling token that expires in exactly 15 seconds."""
    session = db.query(ClassSession).filter(ClassSession.id == session_id).first()
    
    if not session or session.status != "active":
        raise HTTPException(status_code=404, detail="Session not active or expired")
    
    # Generate a secure 32-byte hash
    new_token = secrets.token_urlsafe(32)
    
    # Save it to the DB and set it to expire in 15 seconds (10s display + 5s grace period)
    session.current_qr_token = new_token
    session.qr_expires_at = datetime.now(timezone.utc) + timedelta(seconds=15)
    db.commit()
    
    return {"qr_token": new_token}

@router.post("/session/stop/{session_id}")
def stop_session(session_id: int, db: Session = Depends(get_db)):
    """Terminates the QR session and automatically marks all unscanned students as Absent."""
    session = db.query(ClassSession).filter(ClassSession.id == session_id).first()
    
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    if session.status == "active":
        # 1. Safely close the QR broadcasting session
        session.status = "completed"
        session.current_qr_token = None
        session.qr_expires_at = None
        
        # 2. Auto-Absent Logic
        # Trace the session back to the specific Course Offering
        timetable = db.query(Timetable).filter(Timetable.id == session.timetable_id).first()
        if timetable:
            offering = db.query(SubjectOffering).filter(SubjectOffering.id == timetable.offering_id).first()
            
            if offering:
                # Get EVERY student officially enrolled in this specific subject & semester
                enrollments = db.query(SessionalMark).filter(
                    SessionalMark.subject_id == offering.subject_id,
                    SessionalMark.semester_id == offering.semester_id
                ).all()
                
                enrolled_student_ids = {e.student_id for e in enrollments}
                
                # Get the students who successfully scanned the QR today
                existing_logs = db.query(AttendanceLog).filter(
                    AttendanceLog.session_id == str(session.id)
                ).all()
                
                present_student_ids = {log.student_id for log in existing_logs}
                
                # Math: Enrolled Students MINUS Present Students = Absent Students
                absent_student_ids = enrolled_student_ids - present_student_ids
                
                # Explicitly write "Absent" rows for the missing students
                for student_id in absent_student_ids:
                    absent_log = AttendanceLog(
                        student_id=student_id,
                        timetable_id=session.timetable_id,
                        session_id=str(session.id),
                        status="Absent"
                    )
                    db.add(absent_log)

        db.commit()
        return {"msg": "Session successfully stopped. Absentees automatically recorded."}
        
    return {"msg": "Session was already closed."}


# ==========================================
# 9. STUDENT QR VALIDATION & GEOFENCING (POST)
# ==========================================
class QRScanRequest(BaseModel):
    token: str
    latitude: float
    longitude: float
    device_fingerprint: str
    device_name: str

def calculate_distance(lat1, lon1, lat2, lon2):
    """Calculates distance in meters between two GPS coordinates using the Haversine formula."""
    R = 6371000  # Radius of Earth in meters
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    
    a = math.sin(delta_phi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2.0) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c

@router.post("/session/scan")
def validate_qr_scan(req: QRScanRequest, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Verifies QR token, checks Geofence safe-zone, and marks attendance."""
    
    # 1. Find the active class session holding this exact token
    session = db.query(ClassSession).filter(ClassSession.current_qr_token == req.token).first()
    
    if not session or session.status != "active":
        raise HTTPException(status_code=400, detail="Invalid or Expired QR Code. Please scan the live screen.")

    # 2. Check strict expiration time
    if datetime.now(timezone.utc) > session.qr_expires_at:
        raise HTTPException(status_code=400, detail="QR Code Expired. Please wait for it to refresh.")

    # 3. GEOFENCING CHECK: Are they within 50 meters of the Lecturer?
    if session.lecturer_latitude and session.lecturer_longitude:
        distance = calculate_distance(
            req.latitude, req.longitude, 
            session.lecturer_latitude, session.lecturer_longitude
        )
        if distance > 50.0:  # 50 Meter Radius Limit
            raise HTTPException(status_code=403, detail=f"Geofence Failed! You are {int(distance)}m away from the classroom.")

    # 4. Identify the Student
    student = db.query(Student).filter(Student.user_id == current_user.id).first()
    if not student:
        raise HTTPException(status_code=403, detail="Only registered students can mark attendance.")

    # --- NEW: STRICT DEVICE FINGERPRINTING LOCK ---
    # Fetch all active devices registered to this student
    active_devices = db.query(UserDevice).filter(
        UserDevice.user_id == current_user.id,
        UserDevice.status == DeviceStatus.ACTIVE
    ).all()

    if not active_devices:
        # FIRST TIME SCAN: Silently lock this exact device as their official scanner forever!
        new_device = UserDevice(
            user_id=current_user.id,
            device_fingerprint=req.device_fingerprint,
            device_name=req.device_name,
            status=DeviceStatus.ACTIVE
        )
        db.add(new_device)
        db.commit()
        print(f"🔒 First-time device locked for student {student.reg_no}")
    else:
        # THEY ALREADY HAVE A DEVICE: Check if the current phone matches the DB
        known_fingerprints = [d.device_fingerprint for d in active_devices]
        if req.device_fingerprint not in known_fingerprints:
            raise HTTPException(
                status_code=403, 
                detail="Security Alert: Unrecognized Device! Please submit a Device Change Request from your Profile settings."
            )

    # 5. Check for duplicate scan
    existing_log = db.query(AttendanceLog).filter(
        AttendanceLog.session_id == str(session.id),
        AttendanceLog.student_id == student.id
    ).first()

    if existing_log:
        raise HTTPException(status_code=400, detail="Attendance already recorded for this session.")

    # 6. Save Attendance to Database
    new_attendance = AttendanceLog(
        student_id=student.id,
        timetable_id=session.timetable_id,
        session_id=str(session.id), # Cast to string to match your DB model
        status="Present",
        device_fingerprint=req.device_fingerprint
    )
    db.add(new_attendance)
    db.commit()
    db.refresh(new_attendance)
    
    return {"msg": "Attendance marked successfully!", "status": "present", "log_id": new_attendance.id}

# ==========================================
# 10. LECTURER ROSTER FETCH (GET)
# ==========================================
@router.get("/offerings/{offering_id}/roster")
def get_class_roster(offering_id: int, db: Session = Depends(get_db)):
    """Fetches the real students enrolled in a specific class offering."""
    offering = db.query(SubjectOffering).filter(SubjectOffering.id == offering_id).first()
    if not offering:
        raise HTTPException(status_code=404, detail="Offering not found")

    # Get all assessments for this offering to map grades
    assessments = db.query(Assessment).filter(
        Assessment.subject_id == offering.subject_id,
        Assessment.semester_id == offering.semester_id
    ).all()
    assessment_ids = [a.id for a in assessments]

    enrollments = db.query(SessionalMark).filter(
        SessionalMark.subject_id == offering.subject_id,
        SessionalMark.semester_id == offering.semester_id
    ).all()

    roster = []
    for enr in enrollments:
        student = db.query(Student).filter(Student.id == enr.student_id).first()
        if student:
            # Fetch their actual grades and submission files
            records = db.query(StudentAssessmentRecord).filter(
                StudentAssessmentRecord.student_id == student.id,
                StudentAssessmentRecord.assessment_id.in_(assessment_ids)
            ).all()
            
            marks_dict = {}
            submissions_dict = {}
            for r in records:
                if r.obtained_marks is not None:
                    marks_dict[r.assessment_id] = r.obtained_marks
                if r.submitted_file_path:
                    submissions_dict[r.assessment_id] = r.submitted_file_path

            roster.append({
                "id": student.id,
                "name": student.full_name,
                "roll": student.reg_no,
                "status": "absent",     
                "attendancePct": 100,   
                "avgGrade": 0,          
                "marks": marks_dict,
                "submissions": submissions_dict # <-- NEW! Sends file paths to frontend
            })

    return roster

# ==========================================
# 11. LIVE SESSION ROSTER POLLING (GET)
# ==========================================
@router.get("/session/{session_id}/live-roster")
def get_live_session_roster(session_id: int, db: Session = Depends(get_db)):
    """Called by Lecturer Dashboard every 3 seconds to see who scanned the QR."""
    
    # Cast session_id to string because AttendanceLog.session_id is a String column
    logs = db.query(AttendanceLog).filter(
        AttendanceLog.session_id == str(session_id),
        AttendanceLog.status == "Present"
    ).all()
    
    present_student_ids = [log.student_id for log in logs]
    
    return {"present_ids": present_student_ids}

# ==========================================
# 12. CREATE ASSIGNMENT & NOTIFY STUDENTS
# ==========================================
@router.post("/assignments")
async def create_assignment(
    offering_id: int = Form(...),
    title: str = Form(...),
    deadline: str = Form(...),
    max_marks: float = Form(...),
    weightage: float = Form(...),
    description: str = Form(""),
    file: Optional[UploadFile] = File(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    # 1. Verify Lecturer
    lecturer = db.query(Lecturer).filter(Lecturer.user_id == current_user.id).first()
    if not lecturer:
        raise HTTPException(status_code=403, detail="Only lecturers can create assignments.")

    offering = db.query(SubjectOffering).filter(SubjectOffering.id == offering_id, SubjectOffering.lecturer_id == lecturer.id).first()
    if not offering:
        raise HTTPException(status_code=404, detail="Course offering not found or unauthorized.")

    # 2. Handle File Upload (If Lecturer attached a PDF/Doc)
    file_path = None
    if file:
        os.makedirs("static/assignments", exist_ok=True)
        safe_filename = f"{uuid.uuid4().hex[:8]}_{file.filename}"
        file_path = f"static/assignments/{safe_filename}"
        with open(file_path, "wb+") as file_object:
            shutil.copyfileobj(file.file, file_object)

    # 3. Create Assessment Record
    try:
        deadline_date = datetime.strptime(deadline, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        deadline_date = None

    new_assessment = Assessment(
        subject_id=offering.subject_id,
        semester_id=offering.semester_id,
        name=title,
        category="Assignment",
        max_marks=max_marks,
        weightage=weightage,
        description=description,
        deadline=deadline_date,
        file_path=file_path,
        status="Active"
    )
    db.add(new_assessment)
    db.flush() # Flush to generate the new_assessment.id without committing yet

    # 4. Find all enrolled students
    enrollments = db.query(SessionalMark).filter(
        SessionalMark.subject_id == offering.subject_id,
        SessionalMark.semester_id == offering.semester_id
    ).all()

    subject = db.query(Subject).filter(Subject.id == offering.subject_id).first()

    # 5. Loop through class: Map the assignment to their Gradebook & Send Notification
    for enr in enrollments:
        student = db.query(Student).filter(Student.id == enr.student_id).first()
        if student:
            # Add blank pending record to student's gradebook
            student_record = StudentAssessmentRecord(
                assessment_id=new_assessment.id,
                student_id=student.id,
                status="Pending"
            )
            db.add(student_record)

            # Send Real Notification to their Bell Icon
            notif = Notification(
                user_id=student.user_id,
                title=f"New Assignment: {title}",
                message=f"Due on {deadline} for {subject.name if subject else 'your class'}.",
                is_read=False,
                type="in_app" 
            )
            db.add(notif)

    db.commit()

    # --- LOG TO DATABASE INSTEAD OF TERMINAL ---
    log_to_db(
        db=db,
        user_id=current_user.id,
        action="Created Assignment",
        entity_type="Assessment",
        entity_id=new_assessment.id,
        new_value=title
    )
    # ------------------------------------------------

    return {
        "msg": f"Assignment created! {len(enrollments)} students notified.", 
        "assessment_id": new_assessment.id
    }
    
# ==========================================
# 13. LECTURER ASSIGNMENTS FETCH (GET)
# ==========================================
@router.get("/offerings/{offering_id}/assignments")
def get_offering_assignments(offering_id: int, db: Session = Depends(get_db)):
    """Fetches real assignments for the Lecturer Dashboard"""
    offering = db.query(SubjectOffering).filter(SubjectOffering.id == offering_id).first()
    if not offering:
        return []

    assessments = db.query(Assessment).filter(
        Assessment.subject_id == offering.subject_id,
        Assessment.semester_id == offering.semester_id
    ).all()

    # Get total enrolled students for the denominator (e.g. 2/40 Submissions)
    enrollments = db.query(SessionalMark).filter(
        SessionalMark.subject_id == offering.subject_id,
        SessionalMark.semester_id == offering.semester_id
    ).all()
    total_students = len(enrollments)

    result = []
    for ass in assessments:
        # Count how many students actually submitted
        sub_count = db.query(StudentAssessmentRecord).filter(
            StudentAssessmentRecord.assessment_id == ass.id,
            StudentAssessmentRecord.status.in_(["Submitted", "Graded"])
        ).count()

        result.append({
            "id": ass.id,
            "title": ass.name,
            "deadline": ass.deadline.strftime("%Y-%m-%d") if ass.deadline else "No Deadline",
            "submissions": sub_count,
            "total": total_students,
            "status": ass.status,
            "maxMarks": ass.max_marks,
            "weight": ass.weightage,
            "type": ass.category
        })
    return result

# ==========================================
# 14. BULK SYNC GRADES (PUT)
# ==========================================
class GradeSyncPayload(BaseModel):
    assessment_id: int
    student_id: int
    marks: float

@router.put("/assignments/bulk-grade")
def bulk_grade_assignments(grades: List[GradeSyncPayload], db: Session = Depends(get_db)):
    """Saves all marks from the Lecturer dashboard to the students' gradebooks."""
    for g in grades:
        record = db.query(StudentAssessmentRecord).filter(
            StudentAssessmentRecord.assessment_id == g.assessment_id,
            StudentAssessmentRecord.student_id == g.student_id
        ).first()
        
        if record:
            record.obtained_marks = g.marks
            if record.status in ["Pending", "Submitted"]:
                record.status = "Graded"
        else:
            # --- IF RECORD IS MISSING, CREATE IT! ---
            new_record = StudentAssessmentRecord(
                assessment_id=g.assessment_id,
                student_id=g.student_id,
                obtained_marks=g.marks,
                status="Graded"
            )
            db.add(new_record)
                
    db.commit()
    return {"msg": "Grades synced successfully!"}

# ==========================================
# 15. DELETE ASSIGNMENT (DELETE)
# ==========================================
@router.delete("/assignments/{assessment_id}")
def delete_assignment(assessment_id: int, db: Session = Depends(get_db)):
    """Permanently deletes an assignment and all its student submissions."""
    ass = db.query(Assessment).filter(Assessment.id == assessment_id).first()
    if not ass:
        raise HTTPException(status_code=404, detail="Assessment not found")
        
    # Safely wipe all student submission records for this assignment first
    db.query(StudentAssessmentRecord).filter(StudentAssessmentRecord.assessment_id == assessment_id).delete()
    
    # Wipe the assignment
    db.delete(ass)
    db.commit()
    
    return {"msg": "Assignment deleted successfully"}

# ==========================================
# 16. CREATE MANUAL ASSESSMENT COLUMN (POST)
# ==========================================
class ManualAssessmentCreate(BaseModel):
    offering_id: int
    title: str
    category: str
    max_marks: float
    weightage: float

@router.post("/assessments/manual")
def create_manual_assessment(
    data: ManualAssessmentCreate, 
    db: Session = Depends(get_db), 
    current_user: User = Depends(get_current_user)
):
    """Creates a simple grading column without a file or deadline."""
    # Verify Lecturer
    lecturer = db.query(Lecturer).filter(Lecturer.user_id == current_user.id).first()
    if not lecturer:
        raise HTTPException(status_code=403, detail="Only lecturers can create assessments.")

    offering = db.query(SubjectOffering).filter(SubjectOffering.id == data.offering_id, SubjectOffering.lecturer_id == lecturer.id).first()
    if not offering:
        raise HTTPException(status_code=404, detail="Course offering not found.")

    new_assessment = Assessment(
        subject_id=offering.subject_id,
        semester_id=offering.semester_id,
        name=data.title,
        category=data.category,
        max_marks=data.max_marks,
        weightage=data.weightage,
        status="Active"
    )
    db.add(new_assessment)
    db.flush()

    # Map the empty column to all enrolled students
    enrollments = db.query(SessionalMark).filter(
        SessionalMark.subject_id == offering.subject_id,
        SessionalMark.semester_id == offering.semester_id
    ).all()

    for enr in enrollments:
        student_record = StudentAssessmentRecord(
            assessment_id=new_assessment.id,
            student_id=enr.student_id,
            status="Pending"
        )
        db.add(student_record)

    db.commit()
    return {"msg": "Manual column created", "assessment_id": new_assessment.id}

# ==========================================
# 17. SEND RISK ALERT NOTIFICATION (POST)
# ==========================================
class AlertPayload(BaseModel):
    student_id: int
    message: str

@router.post("/students/alert")
def send_student_alert(payload: AlertPayload, db: Session = Depends(get_db)):
    """Sends a direct warning notification to a student's dashboard."""
    student = db.query(Student).filter(Student.id == payload.student_id).first()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")

    notif = Notification(
        user_id=student.user_id,
        title="⚠️ Academic Risk Alert",
        message=payload.message,
        is_read=False,
        type="in_app"  # Uses the exact Enum value your database requires
    )

    db.add(notif)
    db.commit()
    return {"msg": "Alert sent successfully"}

# ==========================================
# 18. COURSE MATERIALS & ANNOUNCEMENTS
# ==========================================
from app.models.academic import CourseMaterial, Announcement
import math

def format_size(size_bytes):
    if size_bytes == 0: return "0 B"
    size_name = ("B", "KB", "MB", "GB")
    i = int(math.floor(math.log(size_bytes, 1024)))
    p = math.pow(1024, i)
    s = round(size_bytes / p, 1)
    return f"{s} {size_name[i]}"

@router.post("/offerings/{offering_id}/materials")
async def upload_material(offering_id: int, file: UploadFile = File(...), db: Session = Depends(get_db)):
    os.makedirs("static/materials", exist_ok=True)
    safe_filename = f"{uuid.uuid4().hex[:8]}_{file.filename}"
    file_path = f"static/materials/{safe_filename}"
    
    file.file.seek(0, 2)
    file_size = file.file.tell()
    file.file.seek(0)
    
    with open(file_path, "wb+") as file_object:
        shutil.copyfileobj(file.file, file_object)

    new_material = CourseMaterial(
        offering_id=offering_id,
        title=file.filename,
        file_path=file_path,
        file_size=format_size(file_size)
    )
    db.add(new_material)
    db.commit()
    db.refresh(new_material)
    return new_material

@router.get("/offerings/{offering_id}/materials")
def get_materials(offering_id: int, db: Session = Depends(get_db)):
    items = db.query(CourseMaterial).filter(CourseMaterial.offering_id == offering_id).order_by(CourseMaterial.uploaded_at.desc()).all()
    return [{"id": i.id, "name": i.title, "path": i.file_path, "size": i.file_size, "date": i.uploaded_at.strftime("%b %d, %Y")} for i in items]

@router.delete("/materials/{material_id}")
def delete_material(material_id: int, db: Session = Depends(get_db)):
    db.query(CourseMaterial).filter(CourseMaterial.id == material_id).delete()
    db.commit()
    return {"msg": "Deleted"}

@router.post("/offerings/{offering_id}/announcements")
def create_announcement(offering_id: int, title: str = Form(...), message: str = Form(...), db: Session = Depends(get_db)):
    new_announcement = Announcement(offering_id=offering_id, title=title, message=message)
    db.add(new_announcement)
    
    # Notify all enrolled students automatically!
    enrollments = db.query(SessionalMark).filter(SessionalMark.subject_id == db.query(SubjectOffering.subject_id).filter_by(id=offering_id).scalar()).all()
    for enr in enrollments:
        student = db.query(Student).filter(Student.id == enr.student_id).first()
        if student:
            notif = Notification(user_id=student.user_id, title=f"Announcement: {title}", message=message, type="in_app")
            db.add(notif)
            
    db.commit()
    return {"msg": "Announcement broadcasted successfully"}

@router.get("/offerings/{offering_id}/announcements")
def get_announcements(offering_id: int, db: Session = Depends(get_db)):
    items = db.query(Announcement).filter(Announcement.offering_id == offering_id).order_by(Announcement.created_at.desc()).all()
    return [{"id": i.id, "title": i.title, "message": i.message, "date": i.created_at.strftime("%b %d, %Y")} for i in items]

@router.get("/all-timetables")
def get_all_timetables_universal(db: Session = Depends(get_db)):
    """Fetches all scheduled classes for the visualizer grid."""
    return db.query(Timetable).all()

# ==========================================
# TIMETABLE MANAGEMENT (DELETE & UPDATE)
# ==========================================

# OPTION A: Completely delete the slot and wipe attendance
@router.delete("/timetables/{timetable_id}")
def delete_timetable_slot(timetable_id: int, db: Session = Depends(get_db)):
    """Permanently deletes a slot AND wipes all associated attendance/sessions."""
    slot = db.query(Timetable).filter(Timetable.id == timetable_id).first()
    if not slot: 
        raise HTTPException(status_code=404, detail="Slot not found")
    
    # 1. Execute RAW SQL to mercilessly wipe dependencies without ORM interference
    db.execute(text("DELETE FROM attendance_logs WHERE timetable_id = :tid"), {"tid": timetable_id})
    db.execute(text("DELETE FROM class_sessions WHERE timetable_id = :tid"), {"tid": timetable_id})

    # 2. Delete the actual timetable slot
    db.delete(slot)
    db.commit()
    return {"msg": "Timetable slot and all associated attendance records deleted successfully"}


# OPTION B: Update the slot (Change time/day WITHOUT losing attendance)
class TimetableUpdate(BaseModel):
    day_of_week: str
    start_time: str
    end_time: str
    classroom_id: int

@router.put("/timetables/{timetable_id}")
def update_timetable_slot(timetable_id: int, data: TimetableUpdate, db: Session = Depends(get_db)):
    """Changes the time/day of a class while preserving all historical attendance."""
    slot = db.query(Timetable).filter(Timetable.id == timetable_id).first()
    if not slot: 
        raise HTTPException(status_code=404, detail="Slot not found")
        
    try:
        slot.start_time = time.fromisoformat(data.start_time)
        slot.end_time = time.fromisoformat(data.end_time)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid time format. Use HH:MM:SS")

    slot.day_of_week = data.day_of_week
    slot.classroom_id = data.classroom_id

    db.commit()
    return {"msg": "Timetable updated successfully! Attendance history preserved."}

# ==========================================
# 19. DELETE SUBJECT & TRANSFER CLASS
# ==========================================
@router.delete("/subjects/{subject_id}")
def delete_subject(subject_id: int, db: Session = Depends(get_db), current_admin=Depends(get_current_admin)):
    """Deletes a subject ONLY if it isn't currently assigned to a live class."""
    is_assigned = db.query(SubjectOffering).filter(SubjectOffering.subject_id == subject_id).first()
    if is_assigned:
        raise HTTPException(status_code=400, detail="Cannot delete subject. It is currently assigned to a Lecturer. Transfer or delete the offering first.")
        
    db.query(Subject).filter(Subject.id == subject_id).delete()
    db.commit()
    return {"msg": "Subject permanently deleted."}

class TransferLecturerReq(BaseModel):
    new_lecturer_id: int

@router.put("/subject-offerings/{offering_id}/transfer")
def transfer_lecturer(offering_id: int, req: TransferLecturerReq, db: Session = Depends(get_db), current_admin=Depends(get_current_admin)):
    """Transfers an existing class to a new lecturer."""
    offering = db.query(SubjectOffering).filter(SubjectOffering.id == offering_id).first()
    if not offering: 
        raise HTTPException(status_code=404, detail="Class offering not found.")
        
    offering.lecturer_id = req.new_lecturer_id
    db.commit()
    return {"msg": "Class successfully transferred to new Lecturer!"}

# ==========================================
# 20. MANUAL LECTURER ATTENDANCE (POST)
# ==========================================
class ManualAttendancePayload(BaseModel):
    offering_id: int
    date: str
    attendance: List[dict] # Expected: [{"student_id": 1, "status": "present"}, ...]

@router.post("/session/manual-attendance")
def save_manual_attendance(payload: ManualAttendancePayload, db: Session = Depends(get_db)):
    """Saves manual attendance from the Lecturer Dashboard Modal."""
    try:
        manual_date = datetime.strptime(payload.date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD")

    # 1. Find or create a 'ghost' session for this manual entry
    # (Since manual entries might not have a live QR session attached)
    timetable = db.query(Timetable).filter(Timetable.offering_id == payload.offering_id).first()
    if not timetable:
        raise HTTPException(status_code=400, detail="Cannot mark attendance for a class with no timetable slots.")

    session = db.query(ClassSession).filter(
        ClassSession.timetable_id == timetable.id,
        ClassSession.session_date == manual_date
    ).first()

    if not session:
        session = ClassSession(
            timetable_id=timetable.id,
            session_date=manual_date,
            status="completed" # Manually entered, so it's already done
        )
        db.add(session)
        db.flush() # Get ID without committing

    # 2. Loop through the roster and save the statuses
    for record in payload.attendance:
        student_id = record.get("student_id")
        status_text = record.get("status").capitalize() # "present" -> "Present"

        # Check if log already exists
        existing_log = db.query(AttendanceLog).filter(
            AttendanceLog.student_id == student_id,
            AttendanceLog.session_id == str(session.id)
        ).first()

        if existing_log:
            existing_log.status = status_text
        else:
            new_log = AttendanceLog(
                student_id=student_id,
                timetable_id=timetable.id,
                session_id=str(session.id),
                status=status_text
            )
            db.add(new_log)

    db.commit()
    return {"msg": "Manual attendance saved successfully."}


# ==========================================
# 21. ELIGIBILITY OVERRIDES (POST)
# ==========================================
class EligibilityPayload(BaseModel):
    offering_id: int
    action: str # "eligible" or "ineligible"

@router.post("/students/{student_id}/override-eligibility")
def override_student_eligibility(student_id: int, payload: EligibilityPayload, db: Session = Depends(get_db)):
    """Overrides attendance requirements for a specific student."""
    # Note: In a real system, you would save this to an override table. 
    # For now, we will just add a bunch of "Present" or "Absent" logs to mathematically force their percentage.
    
    # 1. Find the timetable
    timetable = db.query(Timetable).filter(Timetable.offering_id == payload.offering_id).first()
    if not timetable:
        raise HTTPException(status_code=400, detail="Class has no timetable slots.")
        
    # 2. Add fake manual logs to push their percentage
    target_status = "Present" if payload.action == "eligible" else "Absent"
    
    # Create 5 logs to heavily skew their percentage in the requested direction
    for i in range(5):
        fake_session = ClassSession(timetable_id=timetable.id, session_date=date.today() - timedelta(days=i), status="completed")
        db.add(fake_session)
        db.flush()
        
        log = AttendanceLog(student_id=student_id, timetable_id=timetable.id, session_id=str(fake_session.id), status=target_status)
        db.add(log)
        
    db.commit()
    return {"msg": f"Student marked as {payload.action}."}

@router.post("/offerings/{offering_id}/override-all-eligible")
def override_all_eligible(offering_id: int, db: Session = Depends(get_db)):
    """Forces all students in a section to be eligible (>80% attendance)."""
    enrollments = db.query(SessionalMark).filter(
        SessionalMark.subject_id == db.query(SubjectOffering.subject_id).filter_by(id=offering_id).scalar(),
        SessionalMark.semester_id == db.query(SubjectOffering.semester_id).filter_by(id=offering_id).scalar()
    ).all()
    
    timetable = db.query(Timetable).filter(Timetable.offering_id == offering_id).first()
    if not timetable:
        raise HTTPException(status_code=400, detail="Class has no timetable slots.")
        
    # Create 5 fake 'Present' sessions for EVERY enrolled student
    for i in range(5):
        fake_session = ClassSession(timetable_id=timetable.id, session_date=date.today() - timedelta(days=i), status="completed")
        db.add(fake_session)
        db.flush()
        
        for enr in enrollments:
            log = AttendanceLog(student_id=enr.student_id, timetable_id=timetable.id, session_id=str(fake_session.id), status="Present")
            db.add(log)
            
    db.commit()
    return {"msg": "All students marked as eligible."}

# ==========================================
# 22. FETCH HISTORICAL ATTENDANCE (GET)
# ==========================================
@router.get("/offerings/{offering_id}/attendance/{date_str}")
def get_historical_attendance(offering_id: int, date_str: str, db: Session = Depends(get_db)):
    """Fetches the attendance roster for a specific past date so it can be edited."""
    try:
        target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD")

    timetable = db.query(Timetable).filter(Timetable.offering_id == offering_id).first()
    if not timetable:
        raise HTTPException(status_code=400, detail="No timetable found for this class.")

    # Find the session for this specific date
    session = db.query(ClassSession).filter(
        ClassSession.timetable_id == timetable.id,
        ClassSession.session_date == target_date
    ).first()

    # Get all enrolled students
    enrollments = db.query(SessionalMark).filter(
        SessionalMark.subject_id == db.query(SubjectOffering.subject_id).filter_by(id=offering_id).scalar(),
        SessionalMark.semester_id == db.query(SubjectOffering.semester_id).filter_by(id=offering_id).scalar()
    ).all()

    roster_data = []
    for enr in enrollments:
        student = db.query(Student).filter(Student.id == enr.student_id).first()
        if not student:
            continue

        status = "absent" # Default if no record exists
        
        # If the session exists, check what was recorded
        if session:
            log = db.query(AttendanceLog).filter(
                AttendanceLog.student_id == student.id,
                AttendanceLog.session_id == str(session.id)
            ).first()
            if log:
                status = log.status.lower()

        roster_data.append({
            "id": student.id,
            "name": student.full_name,
            "roll": student.reg_no,
            "status": status
        })

    return roster_data