from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from datetime import datetime, timezone
from app.api.deps import get_db, get_current_admin, get_current_user  # <-- Added get_current_user

# --- IMPORT ALL NECESSARY MODELS ---
from app.models.system import SysLog, Setting, Notification  # <-- Added Notification
from app.models.academic import SubjectOffering, Subject, Degree, ClassSession, Timetable, Classroom
from app.models.users import Admin, Lecturer, Student, User
from app.models.attendance import Attendance, ExceptionLog
from app.models.performance import SessionalMark  # <-- Added SessionalMark
from app.models.system import Notification

router = APIRouter()

# ==========================================
# EXISTING: DASHBOARD STATS
# ==========================================
@router.get("/dashboard-stats")
def get_admin_dashboard_stats(db: Session = Depends(get_db), current_admin=Depends(get_current_admin)):
    """Fetches real system alerts and global stats for the Admin Overview tab."""
    
    # 1. Fetch Real Alerts from SysLog (Look for warnings or critical errors)
    recent_logs = db.query(SysLog).filter(
        SysLog.severity.in_(['critical', 'warning'])
    ).order_by(SysLog.timestamp.desc()).limit(5).all()

    alerts = []
    for log in recent_logs:
        alerts.append({
            "id": log.id,
            "type": log.severity,  # 'critical' or 'warning'
            "msg": log.action,
            "time": log.timestamp.strftime("%b %d, %I:%M %p") if log.timestamp else "Just now"
        })

    # If the system is healthy and there are no errors, send a clear status!
    if not alerts:
        alerts = [{
            "id": "healthy_01", 
            "type": "nominal", 
            "msg": "All systems operating normally. No security breaches detected.", 
            "time": "Just now"
        }]

    # --- ADMIN SCOPING LOGIC ---
    admin_profile = db.query(Admin).filter(Admin.user_id == current_admin.id).first()
    
    # Safely determine if they are a super admin and get their department
    is_super = False
    dept_id = None
    if admin_profile:
        is_super = "super" in str(admin_profile.role_level).lower()
        dept_id = admin_profile.department_id

    # 2. Fetch Real Active Courses Count (Scoped to Department)
    courses_query = db.query(SubjectOffering).join(Subject).join(Degree)
    
    if not is_super and dept_id:
        # Only count courses belonging to this admin's department
        courses_query = courses_query.filter(Degree.department_id == dept_id)
        
    active_courses = courses_query.filter(SubjectOffering.is_active == True).count()

    return {
        "alerts": alerts,
        "stats": {
            "activeCourses": active_courses,
            "systemHealth": "99.9% Uptime",
            "storageUsed": "Stable"
        }
    }


# ==========================================
# NEW: GEOFENCING & BEACON SECURITY
# ==========================================

# 1. GET ACTIVE BEACONS (RADAR)
@router.get("/geofence/active-beacons")
def get_active_beacons(db: Session = Depends(get_db)):
    """Finds all currently active class sessions to display on the live radar."""
    active_sessions = db.query(ClassSession).filter(ClassSession.status == "active").all()
    
    results = []
    for session in active_sessions:
        # Skip if GPS wasn't recorded
        if not session.lecturer_latitude or not session.lecturer_longitude:
            continue
            
        # Traverse relationships
        tt = db.query(Timetable).filter(Timetable.id == session.timetable_id).first()
        if not tt: continue
        
        offering = db.query(SubjectOffering).filter(SubjectOffering.id == tt.offering_id).first()
        subject = db.query(Subject).filter(Subject.id == offering.subject_id).first()
        lecturer = db.query(Lecturer).filter(Lecturer.id == offering.lecturer_id).first()
        classroom = db.query(Classroom).filter(Classroom.id == tt.classroom_id).first()
        
        # Count present students
        present_count = db.query(Attendance).filter(
            Attendance.class_session_id == session.id,
            Attendance.status == "present"
        ).count()

        results.append({
            "id": session.id,
            "lecturer_name": lecturer.full_name if lecturer else "Unknown",
            "subject_name": subject.name if subject else "Unknown",
            "lat": str(session.lecturer_latitude),
            "lng": str(session.lecturer_longitude),
            "loc": classroom.room_no if classroom else "TBD",
            "students": present_count
        })
        
    return results

# 2. GET VIOLATIONS (SECURITY LOGS)
@router.get("/geofence/violations")
def get_geofence_violations(db: Session = Depends(get_db)):
    """Fetches recent security violations for the geofence."""
    logs = db.query(ExceptionLog).order_by(ExceptionLog.id.desc()).limit(20).all()
    
    results = []
    for log in logs:
        # Find the student who caused the exception
        user = db.query(User).filter(User.id == log.raised_by).first()
        student = db.query(Student).filter(Student.user_id == log.raised_by).first() if user else None
        
        if student:
            # Simple time ago calculator
            time_ago = "Recently"
            if hasattr(log, 'timestamp') and log.timestamp:
                time_diff = datetime.now(timezone.utc) - log.timestamp
                mins = int(time_diff.total_seconds() / 60)
                if mins < 60: time_ago = f"{mins} mins ago"
                else: time_ago = f"{int(mins/60)} hours ago"

            results.append({
                "id": log.id,
                "student_name": student.full_name,
                "roll_no": student.reg_no,
                "distance_away": log.reason or "Out of Bounds",
                "action_taken": "Blocked" if log.resolution_status == "pending" else "Flagged",
                "time_ago": time_ago
            })
            
    return results

# 3. GET GLOBAL SETTINGS
@router.get("/geofence/settings")
def get_geofence_settings(db: Session = Depends(get_db)):
    radius_setting = db.query(Setting).filter(Setting.key_name == "geofence_radius").first()
    strict_setting = db.query(Setting).filter(Setting.key_name == "geofence_strict_mode").first()
    
    return {
        "allowed_radius": int(radius_setting.value) if radius_setting else 20,
        "strict_mode": strict_setting.value.lower() == "true" if strict_setting else True
    }

# 4. UPDATE GLOBAL SETTINGS
@router.post("/geofence/settings")
def update_geofence_settings(data: dict, db: Session = Depends(get_db)):
    # Helper to Upsert Settings
    def upsert_setting(key, val):
        setting = db.query(Setting).filter(Setting.key_name == key).first()
        if setting:
            setting.value = str(val)
        else:
            setting = Setting(key_name=key, value=str(val), category="security")
            db.add(setting)

    upsert_setting("geofence_radius", data.get("allowed_radius", 20))
    upsert_setting("geofence_strict_mode", "true" if data.get("strict_mode", True) else "false")
    
    db.commit()
    return {"msg": "Settings updated"}

# ==========================================
# NEW: COMMUNICATION CENTER
# ==========================================

@router.post("/communication/broadcast")
def send_broadcast(data: dict, db: Session = Depends(get_db), current_admin: User = Depends(get_current_admin)):
    target_type = data.get("target")
    specific_id = data.get("specificId")
    title = data.get("title")
    message = data.get("body")
    
    if not title or not message:
        raise HTTPException(status_code=400, detail="Title and message are required.")
    
    users_to_notify = []
    
    # 1. Gather the target users based on selection
    if target_type == "all":
        users_to_notify = db.query(User).filter(User.is_active == True).all()
    elif target_type == "students":
        users_to_notify = db.query(User).filter(User.role == "student", User.is_active == True).all()
    elif target_type == "lecturers":
        users_to_notify = db.query(User).filter(User.role == "lecturer", User.is_active == True).all()
    elif target_type == "dept" and specific_id:
        dept_id = int(specific_id)
        # Find all students and lecturers in this department
        dept_students = db.query(User).join(Student).join(Degree).filter(Degree.department_id == dept_id).all()
        dept_lecturers = db.query(User).join(Lecturer).filter(Lecturer.department_id == dept_id).all()
        users_to_notify = dept_students + dept_lecturers
    elif target_type == "specific" and specific_id:
        # Check student roll no or lecturer emp code
        student = db.query(Student).filter(Student.reg_no == specific_id).first()
        if student:
            users_to_notify = [db.query(User).filter(User.id == student.user_id).first()]
        else:
            lecturer = db.query(Lecturer).filter(Lecturer.employee_code == specific_id).first()
            if lecturer:
                users_to_notify = [db.query(User).filter(User.id == lecturer.user_id).first()]
                
    if not users_to_notify:
        raise HTTPException(status_code=404, detail="No users found for the selected target.")
        
    # 2. Create the Notifications
    notifs = []
    for u in set(users_to_notify): # Use set to prevent duplicates
        if u:
            notifs.append(Notification(
                user_id=u.id,
                title=title,
                message=message,
                type="in_app"
            ))
    db.bulk_save_objects(notifs)
    
    # 3. Log the Broadcast in SysLog for the History Tab
    target_label = target_type.upper() if target_type != "specific" else specific_id
    if target_type == "dept": target_label = f"DEPT_{specific_id}"
    
    syslog = SysLog(
        user_id=current_admin.id,
        action=f"BROADCAST|{title}|{message}|{target_label}|{len(notifs)}",
        module="COMMUNICATION",
        severity="info"
    )
    db.add(syslog)
    
    db.commit()
    return {"msg": f"Broadcast delivered securely to {len(notifs)} users!"}

@router.get("/communication/history")
def get_broadcast_history(db: Session = Depends(get_db)):
    """Fetches the history of sent broadcasts."""
    logs = db.query(SysLog).filter(
        SysLog.module == "COMMUNICATION", 
        SysLog.action.like("BROADCAST|%")
    ).order_by(SysLog.id.desc()).limit(20).all()
    
    history = []
    for log in logs:
        parts = log.action.split("|")
        if len(parts) >= 5:
            # Calculate time ago
            time_ago = "Recently"
            if hasattr(log, 'timestamp') and log.timestamp:
                time_diff = datetime.now(timezone.utc) - log.timestamp
                mins = int(time_diff.total_seconds() / 60)
                if mins < 60: time_ago = f"{mins} mins ago"
                elif mins < 1440: time_ago = f"{int(mins/60)} hours ago"
                else: time_ago = f"{int(mins/1440)} days ago"
            
            # Format the target label nicely
            target_clean = parts[3].replace("ALL", "All Users").replace("STUDENTS", "All Students").replace("LECTURERS", "All Lecturers")
            if target_clean.startswith("DEPT_"): target_clean = f"Department ID: {target_clean.split('_')[1]}"
            
            history.append({
                "id": log.id,
                "title": parts[1],
                "body": parts[2],
                "target": target_clean,
                "count": parts[4],
                "date": time_ago
            })
    return history

# ==========================================
# NEW: CONFIG & POLICIES
# ==========================================

# 5. GET ACADEMIC SETTINGS
@router.get("/settings/academic")
def get_academic_settings(db: Session = Depends(get_db)):
    """Fetches global academic rules and semester settings."""
    def get_val(key, default):
        s = db.query(Setting).filter(Setting.key_name == key).first()
        return s.value if s else default

    return {
        "min_attendance_pct": int(get_val("min_attendance_pct", 80)),
        "semester_start_date": get_val("semester_start_date", ""),
        "semester_end_date": get_val("semester_end_date", ""),
        "grade_freeze_active": get_val("grade_freeze_active", "false").lower() == "true"
    }

# 6. UPDATE ACADEMIC SETTINGS
@router.post("/settings/academic")
def update_academic_settings(data: dict, db: Session = Depends(get_db)):
    """Updates global academic rules."""
    def upsert_setting(key, val):
        setting = db.query(Setting).filter(Setting.key_name == key).first()
        if setting:
            setting.value = str(val)
        else:
            setting = Setting(key_name=key, value=str(val), category="academic")
            db.add(setting)

    upsert_setting("min_attendance_pct", data.get("min_attendance_pct", 80))
    upsert_setting("semester_start_date", data.get("semester_start_date", ""))
    upsert_setting("semester_end_date", data.get("semester_end_date", ""))
    upsert_setting("grade_freeze_active", "true" if data.get("grade_freeze_active") else "false")
    
    db.commit()
    return {"msg": "Academic policies updated successfully!"}

# ==========================================
# NEW: REPORTS & ANALYTICS
# ==========================================
import csv
from io import StringIO
from fastapi.responses import StreamingResponse

@router.post("/reports/submit-to-admin")
def submit_report_to_admin(data: dict, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Lecturer calls this to officially submit their finalized grades/attendance."""
    offering_id = data.get("offering_id")
    report_type = data.get("report_type") # 'grades' or 'attendance'
    
    offering = db.query(SubjectOffering).filter(SubjectOffering.id == offering_id).first()
    if not offering: raise HTTPException(status_code=404, detail="Course not found.")
    
    subject = db.query(Subject).filter(Subject.id == offering.subject_id).first()
    lecturer = db.query(Lecturer).filter(Lecturer.id == offering.lecturer_id).first()
    
    # 1. Log it as an official System Action
    log = SysLog(
        user_id=current_user.id,
        action=f"REPORT_SUBMITTED|{report_type}|{offering_id}|{subject.name}|{lecturer.full_name}",
        module="REPORTS",
        severity="info"
    )
    db.add(log)
    
    # 2. Notify Admins
    admin_users = db.query(User).filter(User.role == "admin").all()
    for admin in admin_users:
        notif = Notification(
            user_id=admin.id,
            title=f"New {report_type.capitalize()} Report Submitted",
            message=f"{lecturer.full_name} has finalized and submitted the {report_type} report for {subject.name}.",
            type="in_app"
        )
        db.add(notif)
        
    db.commit()
    return {"msg": f"{report_type.capitalize()} officially submitted to Administration!"}


@router.get("/reports/history")
def get_submitted_reports(db: Session = Depends(get_db)):
    """Admin dashboard fetches this to see what lecturers have submitted."""
    logs = db.query(SysLog).filter(SysLog.action.like("REPORT_SUBMITTED%")).order_by(SysLog.id.desc()).all()
    
    reports = []
    for log in logs:
        parts = log.action.split("|")
        if len(parts) >= 5:
            time_str = log.timestamp.strftime("%b %d, %Y") if hasattr(log, 'timestamp') and log.timestamp else "Recently"
            reports.append({
                "id": log.id,
                "type": parts[1], # 'grades' or 'attendance'
                "offering_id": parts[2],
                "subject": parts[3],
                "lecturer": parts[4],
                "date": time_str
            })
    return reports

@router.get("/reports/export/grades/{offering_id}")
def export_grades_csv(offering_id: int, db: Session = Depends(get_db)):
    """Generates a live CSV file of all student grades for a specific course."""
    offering = db.query(SubjectOffering).filter(SubjectOffering.id == offering_id).first()
    if not offering: raise HTTPException(status_code=404, detail="Course not found")
    
    subject = db.query(Subject).filter(Subject.id == offering.subject_id).first()
    
    # Get all students enrolled in this semester/subject
    enrollments = db.query(SessionalMark).filter(
        SessionalMark.subject_id == subject.id,
        SessionalMark.semester_id == offering.semester_id
    ).all()
    
    # Create CSV in memory
    stream = StringIO()
    writer = csv.writer(stream)
    writer.writerow(["Student ID", "Student Name", "Course Code", "Course Name", "Total Score", "Status"])
    
    for enr in enrollments:
        student = db.query(Student).filter(Student.id == enr.student_id).first()
        if student:
            # Simplified mock status logic for the export
            total = enr.total_sessional_marks or 0
            status = "PASS" if total >= 50 else "FAIL"
            writer.writerow([student.reg_no, student.full_name, subject.code, subject.name, total, status])
            
    # Reset stream cursor
    stream.seek(0)
    safe_name = subject.name.replace(" ", "_")
    return StreamingResponse(
        iter([stream.getvalue()]), 
        media_type="text/csv", 
        headers={"Content-Disposition": f"attachment; filename=Grades_{safe_name}.csv"}
    )
    
@router.get("/reports/stats")
def get_global_report_stats(db: Session = Depends(get_db)):
    """Calculates live global passing rates and at-risk student counts."""
    
    # 1. Calculate Average Pass Rate (Based on all Sessional Marks)
    total_enrollments = db.query(SessionalMark).count()
    passed_enrollments = db.query(SessionalMark).filter(SessionalMark.total_sessional_marks >= 50).count()
    
    pass_rate = 100.0  # Default to 100% if no grades exist yet
    if total_enrollments > 0:
        pass_rate = round((passed_enrollments / total_enrollments) * 100, 1)

    # 2. Calculate At-Risk Students 
    # (Count distinct students who have a failing grade in ANY course)
    at_risk_count = db.query(SessionalMark.student_id).filter(
        SessionalMark.total_sessional_marks < 50
    ).distinct().count()

    return {
        "avg_pass_rate": pass_rate,
        "at_risk_count": at_risk_count
    }