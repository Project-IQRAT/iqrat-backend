from ast import List
import csv
from io import StringIO
from sqlalchemy.exc import IntegrityError
from app.models.users import DeviceChangeRequest, RequestStatus, UserDevice, DeviceStatus
import shutil
import os
from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File, Form
from sqlalchemy.orm import Session
from pydantic import BaseModel
from app.core.security import get_password_hash, verify_password 
from app.models.users import User, Student, Lecturer, Admin, UserRole, AdminRole
from app.api.deps import get_db, get_current_admin, get_current_user
from typing import Optional
from pydantic import BaseModel
from app.models.academic import Subject, SubjectOffering, Timetable, Classroom, Section, Semester, SessionBatch, Degree
from app.models.performance import SessionalMark, AttendanceLog, StudentGamification, Assessment, StudentAssessmentRecord
from app.models.attendance import Avatar, AvatarMoodLog
from app.models.system import Notification
from app.ml.predictor import ai_engine
from app.models.performance import PerformancePrediction, ResultStatus

router = APIRouter()


# 1. STUDENT ONBOARDING (MANUAL)
@router.post("/onboard/student")
async def onboard_student(
    full_name: str = Form(...),
    email: str = Form(...),
    roll_no: str = Form(...),
    degree_id: int = Form(None),
    section_id: int = Form(None), # <-- Re-added
    password: str = Form(...),
    contact_no: str = Form(None),
    photo: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin)
):
    if db.query(User).filter(User.email == email).first():
        raise HTTPException(status_code=400, detail="Email already registered")
    if db.query(Student).filter(Student.reg_no == roll_no).first():
        raise HTTPException(status_code=400, detail="Roll Number already exists")

    # --- MISSING PIECE RESTORED: FILE UPLOAD LOGIC ---
    os.makedirs("static/admission_photos", exist_ok=True)
    file_extension = photo.filename.split(".")[-1]
    safe_filename = f"{roll_no}_master.{file_extension}"
    file_location = f"static/admission_photos/{safe_filename}"
    
    with open(file_location, "wb+") as file_object:
        shutil.copyfileobj(photo.file, file_object)

    new_user = User(
        email=email, 
        hashed_password=get_password_hash(password), 
        role=UserRole.STUDENT,
        is_active=True
    )
    db.add(new_user)
    db.flush() # Get ID without committing

    # --- Smartly auto-deduce the Semester and Batch from the Section! ---
    semester_id = None
    session_id = None
    if section_id:
        section = db.query(Section).filter(Section.id == section_id).first()
        if section:
            semester_id = section.semester_id
            semester = db.query(Semester).filter(Semester.id == semester_id).first()
            if semester:
                session_id = semester.session_id

    new_student = Student(
        user_id=new_user.id,
        full_name=full_name,
        reg_no=roll_no,
        degree_id=degree_id,
        section_id=section_id,   
        semester_id=semester_id, 
        session_id=session_id,   
        photo_path=file_location, # <-- Now this exists!
        contact_no=contact_no, 
        status="active"
    )
    db.add(new_student)
    db.commit()

    print(f"EMAIL SENT TO STUDENT: {email}")
    print(f"PASSWORD: {password}")
    
    return {"msg": "Student Onboarded Successfully", "student_id": new_student.id}


# 2. LECTURER ONBOARDING
@router.post("/onboard/lecturer")
def onboard_lecturer(
    full_name: str = Form(...),
    email: str = Form(...),
    employee_code: str = Form(...),
    department_id: int = Form(...),
    password: str = Form(...),
    contact_no: str = Form(None),
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin)
):
    if db.query(User).filter(User.email == email).first():
        raise HTTPException(status_code=400, detail="Email taken")

    new_user = User(email=email, hashed_password=get_password_hash(password), role=UserRole.LECTURER, is_active=True)
    db.add(new_user)
    db.commit()
    db.refresh(new_user)

    new_lecturer = Lecturer(
        user_id=new_user.id,
        full_name=full_name,
        employee_code=employee_code,
        department_id=department_id,
        contact_no=contact_no
    )
    db.add(new_lecturer)
    db.commit()

    return {"msg": "Lecturer Onboarded Successfully", "lecturer_id": new_lecturer.id}


# 3. ADMIN ONBOARDING
@router.post("/onboard/admin")
def onboard_admin(
    full_name: str = Form(...),
    admin_id: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    contact_no: str = Form(None), 
    role_level: str = Form(...), 
    department_id: int = Form(None), 
    permissions: str = Form(None),
    db: Session = Depends(get_db)
    #current_admin: User = Depends(get_current_admin)
):
    #if current_admin.role != UserRole.ADMIN:
        #raise HTTPException(status_code=403, detail="Only Admins can do this")

    # ENFORCE: Only 1 Super Admin Allowed
    if role_level == "super_admin":
        existing_super = db.query(Admin).filter(Admin.role_level == AdminRole.SUPER_ADMIN).first()
        if existing_super:
            raise HTTPException(status_code=400, detail="A Super Admin already exists. Only one is allowed.")

    if db.query(User).filter(User.email == email).first():
        raise HTTPException(status_code=400, detail="Email taken")
        
    if db.query(Admin).filter(Admin.admin_id == admin_id).first():
        raise HTTPException(status_code=400, detail="Admin ID already exists")

    new_user = User(email=email, hashed_password=get_password_hash(password), role=UserRole.ADMIN, is_active=True)
    db.add(new_user)
    db.flush() # Get user ID without committing

    new_admin = Admin(
        user_id=new_user.id,
        admin_id=admin_id,
        full_name=full_name,   
        role_level=role_level,
        department_id=department_id,
        contact_no=contact_no,
        permissions="ALL" if role_level == "super_admin" else permissions
    )
    db.add(new_admin)
    db.commit()
    
    return {"msg": f"{'Super' if role_level == 'super_admin' else 'Department'} Admin Created Successfully", "admin_id": new_admin.id}

# 3.5 AUTO-GENERATE ADMIN ID
@router.get("/next-admin-id")
def get_next_admin_id(db: Session = Depends(get_db), current_admin: User = Depends(get_current_admin)):
    admins = db.query(Admin.admin_id).all()
    max_id = 0
    for adm in admins:
        code = adm[0]
        try:
            if code and code.startswith("ADM-"):
                num = int(code.split("-")[1])
                if num > max_id:
                    max_id = num
        except:
            continue
    next_id = max_id + 1
    return {"next_admin_id": f"ADM-{str(next_id).zfill(3)}"}


# --- BULK CSV ONBOARDING ---
@router.post("/onboard/bulk")
async def onboard_bulk_users(
    role: str = Form(...),
    department_id: int = Form(...),
    degree_id: int = Form(None),
    batch_year: int = Form(None),
    batch_type: str = Form(None),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin)
):
    """Reads a CSV file to mass-enroll students or lecturers."""
    content = await file.read()
    decoded = content.decode('utf-8-sig') # Handles standard CSV formatting
    csv_reader = csv.DictReader(StringIO(decoded))
    
    success_count = 0
    error_count = 0

    for row in csv_reader:
        email = row.get("email")
        full_name = row.get("full_name")
        password = row.get("password", "iqrat123") # Give a default password if blank
        
        if not email or not full_name:
            error_count += 1
            continue
            
        # Skip if email already exists
        if db.query(User).filter(User.email == email).first():
            error_count += 1
            continue
            
        try:
            # Create Base User Account
            role_enum = UserRole.STUDENT if role.lower() == "student" else UserRole.LECTURER
            new_user = User(email=email, hashed_password=get_password_hash(password), role=role_enum, is_active=True)
            db.add(new_user)
            db.flush() # Get the new_user.id without committing yet

            # Create specific profile
            if role.lower() == "student":
                new_student = Student(
                    user_id=new_user.id,
                    full_name=full_name,
                    reg_no=row.get("roll_no"),
                    department_id=department_id,
                    degree_id=degree_id,
                    status="active"
                )
                db.add(new_student)
            else:
                new_lecturer = Lecturer(
                    user_id=new_user.id,
                    full_name=full_name,
                    employee_code=row.get("employee_code"),
                    department_id=department_id
                )
                db.add(new_lecturer)
            
            success_count += 1
        except Exception as e:
            db.rollback()
            error_count += 1
            continue
            
    db.commit()
    return {"msg": f"Upload Complete: {success_count} enrolled, {error_count} failed or skipped."}


# --- SCHEMAS FOR EDITING ---
class UserEdit(BaseModel):
    full_name: str
    email: str
    contact_no: Optional[str] = None
    section_id: Optional[int] = None
    designation: Optional[str] = None
    permissions: Optional[str] = None

# 4. FETCH USERS (SCOPED BY ADMIN ROLE + INCLUDES SYSTEM ID)
@router.get("/all")
def get_all_users(db: Session = Depends(get_db), current_admin: User = Depends(get_current_admin)):
    admin_profile = db.query(Admin).filter(Admin.user_id == current_admin.id).first()
    role_level = str(admin_profile.role_level).lower() if admin_profile else "super_admin"
    is_super = "super" in role_level
    dept_id = admin_profile.department_id if admin_profile else None

    users = []
    
    # Fetch Students
    students_q = db.query(Student, User.email, User.is_active).join(User)
    if not is_super and dept_id:
        # Since we now correctly save degree_id, we just link Student -> Degree -> Department!
        students_q = students_q.join(Degree, Student.degree_id == Degree.id)\
                               .filter(Degree.department_id == dept_id)
        
    for s, email, is_act in students_q.all():
        users.append({
            "id": s.user_id, 
            "name": s.full_name, 
            "system_id": getattr(s, 'reg_no', 'N/A'), 
            "role": "Student", 
            "email": email, 
            "status": "Active" if is_act else "Inactive", 
            "lastLogin": "Recent"
        })

    # Fetch Lecturers (Lecturers DO have a direct department_id)
    lecturers_q = db.query(Lecturer, User.email, User.is_active).join(User)
    if not is_super and dept_id:
        lecturers_q = lecturers_q.filter(Lecturer.department_id == dept_id)
        
    for l, email, is_act in lecturers_q.all():
        users.append({
            "id": l.user_id, 
            "profile_id": l.id, 
            "name": l.full_name, 
            "system_id": getattr(l, 'employee_code', 'N/A'), 
            "role": "Lecturer", 
            "email": email, 
            "status": "Active" if is_act else "Inactive", 
            "lastLogin": "Recent"
        })

    # Fetch Admins (Only Super Admins can see other admins)
    if is_super:
        admins_q = db.query(Admin, User.email, User.is_active).join(User)
        for a, email, is_act in admins_q.all():
            users.append({
                "id": a.user_id, 
                "name": a.full_name, 
                "system_id": getattr(a, 'admin_id', 'N/A'), 
                "role": "Admin", 
                "email": email, 
                "status": "Active" if is_act else "Inactive", 
                "lastLogin": "Recent",
                "permissions": getattr(a, 'permissions', '')
            })
            
    return users

# 5. EDIT USER
@router.put("/{user_id}")
def edit_user(user_id: int, data: UserEdit, db: Session = Depends(get_db), current_admin: User = Depends(get_current_admin)):
    user = db.query(User).filter(User.id == user_id).first()
    if not user: raise HTTPException(status_code=404, detail="User not found")
    
    user.email = data.email
    
    if user.role == UserRole.STUDENT:
        profile = db.query(Student).filter(Student.user_id == user.id).first()
        if profile: 
            profile.full_name = data.full_name
            if data.contact_no: profile.contact_no = data.contact_no
            if data.section_id: profile.section_id = data.section_id
            
    elif user.role == UserRole.LECTURER:
        profile = db.query(Lecturer).filter(Lecturer.user_id == user.id).first()
        if profile:
            profile.full_name = data.full_name
            if data.contact_no: profile.contact_no = data.contact_no
            if data.designation: profile.designation = data.designation
            
    elif user.role == UserRole.ADMIN:
        profile = db.query(Admin).filter(Admin.user_id == user_id).first()
        if profile: 
            profile.full_name = data.full_name
            if data.contact_no: profile.contact_no = data.contact_no
            if data.permissions is not None: profile.permissions = data.permissions
            
    db.commit()
    return {"msg": "User updated successfully!"}

# 6. DELETE USER
@router.delete("/{user_id}")
def delete_user(user_id: int, db: Session = Depends(get_db), current_admin: User = Depends(get_current_admin)):
    user = db.query(User).filter(User.id == user_id).first()
    if not user: raise HTTPException(status_code=404, detail="User not found")
    
    # 1. Look for the student profile
    student = db.query(Student).filter(Student.user_id == user_id).first()
    
    # 2. If it's a student, wipe their specific relational data FIRST
    if student:
        # Wipe Gamification
        db.query(StudentGamification).filter(StudentGamification.student_id == student.id).delete()
        # Wipe Device Requests
        db.query(DeviceChangeRequest).filter(DeviceChangeRequest.student_id == student.id).delete()
        # Wipe Grades/Enrollment
        db.query(SessionalMark).filter(SessionalMark.student_id == student.id).delete()
        db.query(StudentAssessmentRecord).filter(StudentAssessmentRecord.student_id == student.id).delete()
        # Wipe Attendance
        db.query(AttendanceLog).filter(AttendanceLog.student_id == student.id).delete()
        
    # 3. Look for the lecturer profile
    lecturer = db.query(Lecturer).filter(Lecturer.user_id == user_id).first()
    if lecturer:
        # Note: If a lecturer is currently assigned to a live course offering, 
        # deleting them will cause an error unless you reassign the course first!
        # For now, we will safely delete the base profile.
        pass

    # 4. Now safely delete the core profiles
    db.query(Student).filter(Student.user_id == user_id).delete()
    db.query(Lecturer).filter(Lecturer.user_id == user_id).delete()
    db.query(Admin).filter(Admin.user_id == user_id).delete()
    
    # 5. Delete global relational data
    db.query(UserDevice).filter(UserDevice.user_id == user_id).delete()
    db.query(Notification).filter(Notification.user_id == user_id).delete()
    
    # 6. Finally delete the core user account
    db.delete(user)
    db.commit()
    
    return {"msg": "User and all associated records deleted."}

# 6. AUTO-GENERATE ROLL NUMBER
@router.get("/next-roll-no")
def get_next_roll_no(
    degree_code: str, 
    year: str,        
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin)
):
    search_pattern = f"%-{degree_code.upper()}-{year}"
    students = db.query(Student.reg_no).filter(Student.reg_no.like(search_pattern)).all()
    
    max_id = 0
    for student in students:
        reg_no = student[0]
        try:
            prefix = reg_no.split("-")[0]
            curr_id = int(prefix)
            if curr_id > max_id:
                max_id = curr_id
        except (ValueError, IndexError):
            continue
            
    next_id = max_id + 1
    next_id_str = str(next_id).zfill(4)
    next_roll_no = f"{next_id_str}-{degree_code.upper()}-{year}"
    
    return {
        "next_roll_no": next_roll_no, 
        "numeric_id": next_id 
    }
    
# 6.5 AUTO-GENERATE EMPLOYEE ID
@router.get("/next-emp-id")
def get_next_emp_id(db: Session = Depends(get_db), current_admin: User = Depends(get_current_admin)):
    lecturers = db.query(Lecturer.employee_code).all()
    max_id = 0
    for lec in lecturers:
        code = lec[0]
        try:
            if code and code.startswith("EMP-"):
                num = int(code.split("-")[1])
                if num > max_id:
                    max_id = num
        except:
            continue
            
    next_id = max_id + 1
    next_emp_id = f"EMP-{str(next_id).zfill(3)}"
    return {"next_emp_id": next_emp_id}

# 7. BATCH ENROLLMENT
class BulkEnrollment(BaseModel):
    section_id: int
    semester_id: int
    subject_id: int

@router.post("/enroll-section")
def batch_enroll_section(data: dict, db: Session = Depends(get_db)):
    semester_id = data.get("semester_id")
    section_id = data.get("section_id")
    subject_id = data.get("subject_id")
    
    # 1. Find all students in this section
    students = db.query(Student).filter(Student.section_id == section_id).all()
    
    enrolled_count = 0
    for student in students:
        # --- NEW: STRICT DUPLICATE CHECK ---
        exists = db.query(SessionalMark).filter(
            SessionalMark.student_id == student.id,
            SessionalMark.subject_id == subject_id,
            SessionalMark.semester_id == semester_id
        ).first()
        
        # Only create a record if one doesn't already exist!
        if not exists:
            new_enrollment = SessionalMark(
                student_id=student.id,
                subject_id=subject_id,
                semester_id=semester_id,
                midterm_marks=0,
                total_sessional_marks=0
            )
            db.add(new_enrollment)
            enrolled_count += 1
            
    db.commit()
    return {"msg": f"Successfully enrolled {enrolled_count} students (Skipped existing)."}


# 8. STUDENT DASHBOARD COURSES
@router.get("/me/courses")
def get_my_courses(email: str, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == email).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
        
    student = db.query(Student).filter(Student.user_id == user.id).first()
    if not student:
        raise HTTPException(status_code=404, detail="Student profile not found")

    enrollments = db.query(SessionalMark).filter(SessionalMark.student_id == student.id).all()
    
    courses_data = []
    for enr in enrollments:
        sub = db.query(Subject).filter(Subject.id == enr.subject_id).first()
        if sub:
            offering = db.query(SubjectOffering).filter(
                SubjectOffering.subject_id == sub.id,
                SubjectOffering.semester_id == enr.semester_id
            ).first()
            
            lecturer_name = "TBD"
            if offering:
                lecturer = db.query(Lecturer).filter(Lecturer.id == offering.lecturer_id).first()
                if lecturer:
                    lecturer_name = lecturer.full_name
            
            section_display = f"Section {student.section_id}"

            logs = []
            if offering:
                tts = db.query(Timetable).filter(Timetable.offering_id == offering.id).all()
                tt_ids = [t.id for t in tts]
                logs = db.query(AttendanceLog).filter(
                    AttendanceLog.student_id == student.id,
                    AttendanceLog.timetable_id.in_(tt_ids),
                    AttendanceLog.status == "Present"
                ).all()

            presents = len(logs)
            total_classes = 30
            attendance_pct = int((presents / total_classes) * 100) if presents > 0 else 0

            courses_data.append({
                "offering_id": offering.id if offering else None,
                "name": sub.name,
                "code": sub.code,
                "section": section_display,
                "lecturer": lecturer_name, 
                "attendance": attendance_pct, 
                "presents": presents,
                "absents": total_classes - presents,
                "leaves": 0
            })
            
    return courses_data


# 9. STUDENT TIMETABLE
@router.get("/me/timetable")
def get_my_timetable(email: str, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == email).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
        
    student = db.query(Student).filter(Student.user_id == user.id).first()
    
    enrollments = db.query(SessionalMark).filter(SessionalMark.student_id == student.id).all()
    schedule = []
    
    for enr in enrollments:
        offering = db.query(SubjectOffering).filter(
            SubjectOffering.subject_id == enr.subject_id,
            SubjectOffering.semester_id == enr.semester_id
        ).first()
        
        if offering:
            sub = db.query(Subject).filter(Subject.id == offering.subject_id).first()
            lec = db.query(Lecturer).filter(Lecturer.id == offering.lecturer_id).first()
            
            slots = db.query(Timetable).filter(Timetable.offering_id == offering.id).all()
            
            for slot in slots:
                room = db.query(Classroom).filter(Classroom.id == slot.classroom_id).first()
                
                schedule.append({
                    "id": slot.id,
                    "day": slot.day_of_week,
                    "start": slot.start_time.strftime("%I:%M %p"),
                    "end": slot.end_time.strftime("%I:%M %p"),
                    "subject": sub.name if sub else "Unknown",
                    "code": sub.code if sub else "---",
                    "teacher": lec.full_name if lec else "TBD",
                    "room": room.room_no if room else "TBD"
                })
                
    return schedule


# 10. LECTURER DASHBOARD FETCHERS
@router.get("/me/lecturer/courses")
def get_lecturer_courses(email: str, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == email).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
        
    lecturer = db.query(Lecturer).filter(Lecturer.user_id == user.id).first()
    if not lecturer:
        raise HTTPException(status_code=404, detail="Lecturer profile not found")

    offerings = db.query(SubjectOffering).filter(SubjectOffering.lecturer_id == lecturer.id).all()
    
    courses_data = []
    for off in offerings:
        sub = db.query(Subject).filter(Subject.id == off.subject_id).first()
        if sub:
            courses_data.append({
                "id": off.id, 
                "code": sub.code,
                "name": sub.name,
                "section": "All Sections" 
            })
            
    return courses_data

@router.get("/me/lecturer/timetable")
def get_lecturer_timetable(email: str, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == email).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
        
    lecturer = db.query(Lecturer).filter(Lecturer.user_id == user.id).first()
    if not lecturer:
        raise HTTPException(status_code=404, detail="Lecturer profile not found")
        
    offerings = db.query(SubjectOffering).filter(SubjectOffering.lecturer_id == lecturer.id).all()
    offering_ids = [o.id for o in offerings]
    
    slots = db.query(Timetable).filter(Timetable.offering_id.in_(offering_ids)).all()
    
    schedule = []
    for slot in slots:
        off = db.query(SubjectOffering).filter(SubjectOffering.id == slot.offering_id).first()
        sub = db.query(Subject).filter(Subject.id == off.subject_id).first()
        room = db.query(Classroom).filter(Classroom.id == slot.classroom_id).first()
        
        schedule.append({
            "timetable_id": slot.id, 
            "offering_id": off.id,
            "day": slot.day_of_week,
            "start": slot.start_time.strftime("%I:%M %p"),
            "end": slot.end_time.strftime("%I:%M %p"),
            "code": sub.code if sub else "---",
            "name": sub.name if sub else "Unknown",
            "room": room.room_no if room else "TBD",
            "type": "Lecture"
        })
        
    return schedule


# 11. STUDENT ATTENDANCE HISTORY
@router.get("/me/attendance")
def get_my_attendance(email: str, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == email).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
        
    student = db.query(Student).filter(Student.user_id == user.id).first()
    
    logs = db.query(AttendanceLog).filter(AttendanceLog.student_id == student.id).order_by(AttendanceLog.scan_time.desc()).all()
    
    history = []
    for i, log in enumerate(logs):
        tt = db.query(Timetable).filter(Timetable.id == log.timetable_id).first()
        sub_code = "---"
        if tt:
            offering = db.query(SubjectOffering).filter(SubjectOffering.id == tt.offering_id).first()
            if offering:
                sub = db.query(Subject).filter(Subject.id == offering.subject_id).first()
                if sub:
                    sub_code = sub.code

        history.append({
            "sr": len(logs) - i, 
            "date": log.scan_time.strftime("%b %d, %Y • %I:%M %p"),
            "status": log.status,
            "subject_code": sub_code
        })
        
    return history


# 12. STUDENT DASHBOARD STATS
@router.get("/me/dashboard-stats")
def get_dashboard_stats(email: str, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == email).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
        
    student = db.query(Student).filter(Student.user_id == user.id).first()
    
    gamification = db.query(StudentGamification).filter(StudentGamification.student_id == student.id).first()
    if not gamification:
        gamification = StudentGamification(student_id=student.id, xp_points=0, current_streak=0)
        db.add(gamification)
        db.commit()
        db.refresh(gamification)

    level = (gamification.xp_points // 100) + 1
    
    if level <= 5: badge = "Novice Learner"
    elif level <= 15: badge = "Scholar"
    elif level <= 49: badge = "Dean's List Elite"
    else: badge = "Legend of GCU"

    all_logs = db.query(AttendanceLog).filter(AttendanceLog.student_id == student.id).all()
    total_scans = len(all_logs)
    total_presents = len([l for l in all_logs if l.status == "Present"])
    
    avg_attendance = 100 
    if total_scans > 0:
        avg_attendance = int((total_presents / total_scans) * 100)

    # --- NEW: AVATAR & MOOD DATABASE LOGIC ---
    if total_scans == 0: new_mood = "focused"
    elif avg_attendance >= 90: new_mood = "happy"
    elif avg_attendance >= 80: new_mood = "improving"
    elif avg_attendance >= 70: new_mood = "focused"
    elif avg_attendance >= 60: new_mood = "lowering"
    else: new_mood = "stressed"

    # Get or create the Avatar record
    avatar = db.query(Avatar).filter(Avatar.student_id == student.id).first()
    if not avatar:
        avatar = Avatar(student_id=student.id, avatar_style=new_mood, level=level, xp_points=gamification.xp_points)
        db.add(avatar)
        db.commit()
        db.refresh(avatar)
    
    # Check if the mood has changed since the last time!
    if avatar.avatar_style != new_mood:
        # 1. Log the emotional shift into the database
        reason = f"Attendance shifted to {avg_attendance}%"
        mood_log = AvatarMoodLog(avatar_id=avatar.id, mood=new_mood, trigger_reason=reason)
        db.add(mood_log)
        
        # 2. Update current state
        avatar.avatar_style = new_mood
        db.commit()
    # ------------------------------------------

    section_student_ids = [s.id for s in db.query(Student).filter(Student.section_id == student.section_id).all()]
    
    all_gami = db.query(StudentGamification).filter(
        StudentGamification.student_id.in_(section_student_ids)
    ).order_by(StudentGamification.xp_points.desc()).all()
    
    my_rank = 1
    for i, g in enumerate(all_gami):
        if g.student_id == student.id:
            my_rank = i + 1
            break
            
    top_10 = []
    for i in range(min(10, len(all_gami))):
        s = db.query(Student).filter(Student.id == all_gami[i].student_id).first()
        if s:
            top_10.append({"name": s.full_name, "roll": s.reg_no, "xp": all_gami[i].xp_points, "rank": i + 1})
    
    while len(top_10) < 3:
        top_10.append({"name": "---", "roll": "---", "xp": 0, "rank": len(top_10) + 1})

    return {
        "xp_points": gamification.xp_points,
        "level": level,
        "badge": badge,
        "current_streak": gamification.current_streak,
        "avg_attendance": avg_attendance,
        "rank": my_rank,
        "top_10_students": top_10,
        "current_mood": avatar.avatar_style # <-- Sending the official DB mood to React!
    }


# 13. STUDENT GRADES FETCH
@router.get("/me/grades")
def get_my_grades(email: str, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == email).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
        
    student = db.query(Student).filter(Student.user_id == user.id).first()
    
    enrollments = db.query(SessionalMark).filter(SessionalMark.student_id == student.id).all()
    
    result = []
    for enr in enrollments:
        subject = db.query(Subject).filter(Subject.id == enr.subject_id).first()
        if not subject:
            continue
            
        assessments = db.query(Assessment).filter(
            Assessment.subject_id == subject.id,
            Assessment.semester_id == enr.semester_id
        ).all()
        
        marks_data = []
        for ass in assessments:
            record = db.query(StudentAssessmentRecord).filter(
                StudentAssessmentRecord.assessment_id == ass.id,
                StudentAssessmentRecord.student_id == student.id
            ).first()
            
            obtained = record.obtained_marks if record and record.obtained_marks is not None else None
            status = record.status if record else "Pending"
            
            marks_data.append({
                "id": ass.id,
                "name": ass.name,             
                "category": ass.category,     
                "total_marks": ass.max_marks, 
                "obtained_marks": obtained,
                "lecturer_file_path": ass.file_path,  # <-- NEW: Allows student to download the question!
                "status": status                      # <-- NEW: Tracks if they have submitted
            })
            
        result.append({
            "subject_id": subject.id,
            "subject_code": subject.code,
            "subject_name": subject.name,
            "assessments": marks_data
        })
        
    return result


# ==========================================
# 14. GET FULL PROFILE & SETTINGS
# ==========================================
@router.get("/me/profile")
def get_my_profile(email: str, db: Session = Depends(get_db)):
    """Fetches all profile details dynamically for Student, Lecturer, or Admin"""
    user = db.query(User).filter(User.email == email).first()
    if not user: raise HTTPException(status_code=404, detail="User not found")
        
    if user.role == UserRole.STUDENT:
        profile_data = db.query(Student).filter(Student.user_id == user.id).first()
        identifier = profile_data.reg_no if profile_data else ""
    elif user.role == UserRole.LECTURER:
        profile_data = db.query(Lecturer).filter(Lecturer.user_id == user.id).first()
        identifier = profile_data.employee_code if profile_data else ""
    elif user.role == UserRole.ADMIN:
        profile_data = db.query(Admin).filter(Admin.user_id == user.id).first()
        identifier = profile_data.admin_id if profile_data else ""

    return {
        "full_name": getattr(profile_data, "full_name", ""),
        "email": user.email,
        "reg_no": identifier,
        "contact_no": getattr(profile_data, "contact_no", ""),
        "photo_path": getattr(profile_data, "photo_path", None),
        "theme_preference": getattr(profile_data, "theme_preference", "default") if hasattr(profile_data, "theme_preference") else "default",
        "notify_class_reminders": getattr(profile_data, "notify_class_reminders", True) if hasattr(profile_data, "notify_class_reminders") else True,
        "notify_assignment_deadlines": getattr(profile_data, "notify_assignment_deadlines", True) if hasattr(profile_data, "notify_assignment_deadlines") else True
    }

# ==========================================
# 15. UPDATE PROFILE (FORM)
# ==========================================
@router.put("/me/profile")
async def update_my_profile(
    current_email: str = Form(...),
    full_name: str = Form(...),
    new_email: str = Form(...),
    contact_no: str = Form(""),
    photo: UploadFile = File(None),
    db: Session = Depends(get_db)
):
    user = db.query(User).filter(User.email == current_email).first()
    if not user: raise HTTPException(status_code=404, detail="User not found")

    if new_email != current_email:
        existing = db.query(User).filter(User.email == new_email).first()
        if existing: raise HTTPException(status_code=400, detail="Email already in use.")
        user.email = new_email

    if user.role == UserRole.STUDENT:
        profile_data = db.query(Student).filter(Student.user_id == user.id).first()
        identifier = profile_data.reg_no
    elif user.role == UserRole.LECTURER:
        profile_data = db.query(Lecturer).filter(Lecturer.user_id == user.id).first()
        identifier = profile_data.employee_code
    elif user.role == UserRole.ADMIN:
        profile_data = db.query(Admin).filter(Admin.user_id == user.id).first()
        identifier = profile_data.admin_id

    profile_data.full_name = full_name
    profile_data.contact_no = contact_no

    if photo:
        os.makedirs("static/profile_photos", exist_ok=True)
        file_location = f"static/profile_photos/{identifier}_master.jpg"
        with open(file_location, "wb+") as file_object:
            shutil.copyfileobj(photo.file, file_object)
        if hasattr(profile_data, "photo_path"):
            profile_data.photo_path = file_location

    db.commit()
    return {"msg": "Profile updated successfully!", "new_email": user.email}

# ==========================================
# 16. UPDATE NOTIFICATIONS & THEMES (JSON)
# ==========================================
class SettingsUpdate(BaseModel):
    theme_preference: str
    notify_class_reminders: bool
    notify_assignment_deadlines: bool

@router.put("/me/settings")
def update_settings(email: str, settings: SettingsUpdate, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == email).first()
    if not user: raise HTTPException(status_code=404, detail="User not found")
        
    if user.role == UserRole.STUDENT:
        profile_data = db.query(Student).filter(Student.user_id == user.id).first()
    else:
        profile_data = db.query(Lecturer).filter(Lecturer.user_id == user.id).first()

    profile_data.theme_preference = settings.theme_preference
    profile_data.notify_class_reminders = settings.notify_class_reminders
    profile_data.notify_assignment_deadlines = settings.notify_assignment_deadlines

    db.commit()
    return {"msg": "Settings saved successfully!"}

# ==========================================
# 17. UPDATE PASSWORD (JSON)
# ==========================================
class PasswordUpdate(BaseModel):
    current_password: str
    new_password: str

@router.put("/me/password")
def update_password(email: str, passwords: PasswordUpdate, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == email).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Securely check if the old password they typed matches the database
    if not verify_password(passwords.current_password, user.hashed_password):
        raise HTTPException(status_code=400, detail="Incorrect current password.")

    # Hash the new password and save it
    user.hashed_password = get_password_hash(passwords.new_password)
    db.commit()
    return {"msg": "Password updated successfully!"}

# ==========================================
# 18. FETCH NOTIFICATIONS (GET)
# ==========================================
@router.get("/me/notifications")
def get_my_notifications(email: str, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == email).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
        
    # Updated to use 'sent_at' based on your system.py model!
    alerts = db.query(Notification).filter(Notification.user_id == user.id).order_by(Notification.sent_at.desc()).limit(5).all()
    
    return [
        {
            "id": a.id, 
            "title": a.title, 
            "message": a.message, 
            "is_read": a.is_read, 
            "time": a.sent_at.strftime("%b %d, %I:%M %p") if a.sent_at else "Just now"
        } 
        for a in alerts
    ]
    
# ==========================================
# 19. DEVICE MANAGEMENT (ADMIN)
# ==========================================
@router.get("/device-requests")
def get_device_requests(db: Session = Depends(get_db)):
    """Fetches all pending device change requests for the Admin dashboard"""
    requests = db.query(DeviceChangeRequest).filter(DeviceChangeRequest.status == RequestStatus.PENDING).all()
    result = []
    
    for req in requests:
        student = db.query(Student).filter(Student.id == req.student_id).first()
        if student:
            result.append({
                "id": req.id,
                "name": student.full_name,
                "id_no": student.reg_no,
                "role": "Student",
                "device": req.new_device_fingerprint[:12] + "...", # Masked for UI
                "reason": req.reason or "Change requested",
                "date": req.requested_at.strftime("%Y-%m-%d") if req.requested_at else "Today"
            })
            
    return result

@router.post("/device-requests/{req_id}/approve")
def approve_device_request(req_id: int, db: Session = Depends(get_db)):
    """Approves a device, revoking old ones and authorizing the new fingerprint"""
    req = db.query(DeviceChangeRequest).filter(DeviceChangeRequest.id == req_id).first()
    if not req: raise HTTPException(status_code=404, detail="Request not found")
    
    req.status = RequestStatus.APPROVED
    
    # Safely get the user_id linked to this student
    student = db.query(Student).filter(Student.id == req.student_id).first()
    
    if student:
        # Revoke all their old devices
        old_devices = db.query(UserDevice).filter(UserDevice.user_id == student.user_id).all()
        for d in old_devices:
            d.status = DeviceStatus.REVOKED
            
        # Add the new requested device as Active!
        new_device = UserDevice(
            user_id=student.user_id,
            device_fingerprint=req.new_device_fingerprint,
            status=DeviceStatus.ACTIVE
        )
        db.add(new_device)
        
    db.commit()
    return {"msg": "Device approved!"}

@router.post("/device-requests/{req_id}/reject")
def reject_device_request(req_id: int, db: Session = Depends(get_db)):
    """Denies a device change request"""
    req = db.query(DeviceChangeRequest).filter(DeviceChangeRequest.id == req_id).first()
    if not req: raise HTTPException(status_code=404, detail="Request not found")
    
    req.status = RequestStatus.REJECTED
    db.commit()
    return {"msg": "Device rejected!"}

# ==========================================
# 20. STUDENT SUBMIT DEVICE CHANGE REQUEST
# ==========================================
class DeviceChangeReq(BaseModel):
    new_device_fingerprint: str
    reason: str

@router.post("/me/device-request")
def request_device_change(req: DeviceChangeReq, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Allows a student to formally request a device change from the Admin"""
    student = db.query(Student).filter(Student.user_id == current_user.id).first()
    if not student:
        raise HTTPException(status_code=403, detail="Only students can request device changes.")
    
    # Block them if they already have an unanswered request pending
    existing = db.query(DeviceChangeRequest).filter(
        DeviceChangeRequest.student_id == student.id,
        DeviceChangeRequest.status == RequestStatus.PENDING
    ).first()
    
    if existing:
        raise HTTPException(status_code=400, detail="You already have a request pending admin approval.")

    # Create the request for the Admin Dashboard
    new_req = DeviceChangeRequest(
        student_id=student.id,
        new_device_fingerprint=req.new_device_fingerprint,
        reason=req.reason,
        status=RequestStatus.PENDING
    )
    db.add(new_req)
    db.commit()
    
    return {"msg": "Request sent! Please wait for the Admin to approve your new device."}

# ==========================================
# 21. SUBMIT ASSIGNMENT (STUDENT)
# ==========================================
@router.post("/me/submit-assignment")
async def submit_assignment(
    email: str = Form(...),
    assessment_id: int = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db)
):
    """Catches the student's answer file and updates their gradebook record"""
    
    # 1. Find the User via Email
    user = db.query(User).filter(User.email == email).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
        
    # FIX: We now use user.id instead of the old current_user.id
    student = db.query(Student).filter(Student.user_id == user.id).first()
    if not student:
        raise HTTPException(status_code=403, detail="Only students can submit assignments.")

    # 2. Find the specific assessment record in their gradebook
    record = db.query(StudentAssessmentRecord).filter(
        StudentAssessmentRecord.assessment_id == assessment_id,
        StudentAssessmentRecord.student_id == student.id
    ).first()

    if not record:
        raise HTTPException(status_code=404, detail="Assessment record not found.")
        
    if record.status == "Graded":
        raise HTTPException(status_code=400, detail="Cannot resubmit an assignment that has already been graded.")

    # 3. Generate a safe, unique filename and save the file
    import uuid
    from datetime import datetime, timezone
    
    os.makedirs("static/submissions", exist_ok=True)
    safe_filename = f"{uuid.uuid4().hex[:8]}_{file.filename}"
    file_path = f"static/submissions/{safe_filename}"
    
    with open(file_path, "wb+") as file_object:
        shutil.copyfileobj(file.file, file_object)

    # 4. Update the database record with the file path and timestamp
    record.submitted_file_path = file_path
    record.submitted_at = datetime.now(timezone.utc)
    record.status = "Submitted"
    
    db.commit()
    
    return {"msg": "Assignment submitted successfully!", "file_path": file_path}

# ==========================================
# 22. MARK NOTIFICATIONS AS READ (PUT)
# ==========================================
class MarkReadRequest(BaseModel):
    notification_ids: list[int]

@router.put("/me/notifications/read")
def mark_notifications_read(req: MarkReadRequest, db: Session = Depends(get_db)):
    """Marks a batch of notifications as read when the user opens the bell menu."""
    db.query(Notification).filter(Notification.id.in_(req.notification_ids)).update(
        {"is_read": True}, synchronize_session=False
    )
    db.commit()
    return {"msg": "Notifications marked as read"}

# ==========================================
# 23. REPEAT SUBJECT / SINGLE ENROLLMENT
# ==========================================
class SingleEnrollRequest(BaseModel):
    reg_no: str
    subject_id: int
    semester_id: int
    section_id: int = None

@router.post("/enroll-repeat")
def enroll_repeat_subject(data: SingleEnrollRequest, db: Session = Depends(get_db), current_admin: User = Depends(get_current_admin)):
    # 1. Find the student by Roll Number
    student = db.query(Student).filter(Student.reg_no == data.reg_no).first()
    if not student:
        raise HTTPException(status_code=404, detail="Student Roll Number not found.")
        
    # 2. Check if they are already enrolled in this exact subject and semester
    exists = db.query(SessionalMark).filter(
        SessionalMark.student_id == student.id,
        SessionalMark.subject_id == data.subject_id,
        SessionalMark.semester_id == data.semester_id
    ).first()
    
    if exists:
        raise HTTPException(status_code=400, detail="Student is already enrolled in this subject for this semester.")
        
    # 3. Create the enrollment record
    new_enrollment = SessionalMark(
        student_id=student.id,
        subject_id=data.subject_id,
        semester_id=data.semester_id,
        midterm_marks=0,
        total_sessional_marks=0
    )
    db.add(new_enrollment)
    db.commit()
    
    return {"msg": f"Successfully enrolled {student.reg_no} in the repeat subject!"}

# ==========================================
# 24. AI PREDICTIVE ANALYTICS ENDPOINT
# ==========================================
@router.get("/me/predictions")
def get_ai_predictions(email: str, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == email).first()
    if not user: raise HTTPException(status_code=404, detail="User not found")
        
    student = db.query(Student).filter(Student.user_id == user.id).first()
    if not student: return []

    enrollments = db.query(SessionalMark).filter(SessionalMark.student_id == student.id).all()
    predictions_data = []
    
    for enr in enrollments:
        subject = db.query(Subject).filter(Subject.id == enr.subject_id).first()
        if not subject: continue

        # 1. SAFER QUERY: Fetch offering IDs first to avoid subquery crashes
        offerings = db.query(SubjectOffering.id).filter(
            SubjectOffering.subject_id == subject.id, 
            SubjectOffering.semester_id == enr.semester_id
        ).all()
        offering_ids = [o[0] for o in offerings]

        logs = []
        if offering_ids:
            # Safely join Timetable and fetch logs
            logs = db.query(AttendanceLog).join(Timetable, AttendanceLog.timetable_id == Timetable.id).filter(
                AttendanceLog.student_id == student.id,
                Timetable.offering_id.in_(offering_ids)
            ).order_by(AttendanceLog.scan_time.asc()).all()

        # Convert text statuses to ML sequence (1 for Present, 0 for Absent/Late)
        att_seq = [1 if log.status == "Present" else 0 for log in logs]
        
        # 2. Feed sequence to PyTorch LSTM (Default to 100% if no classes held yet)
        predicted_att_pct = ai_engine.predict_attendance(att_seq) if att_seq else 100.0

        # 2. Fetch Assessments to calculate Sessional vs Final
        assessments = db.query(Assessment).filter(
            Assessment.subject_id == subject.id,
            Assessment.semester_id == enr.semester_id
        ).all()

        sessional_max = 0.0
        sessional_obtained = 0.0
        final_exam_obtained = 0.0 # Will remain 0 if not taken yet
        
        for ass in assessments:
            record = db.query(StudentAssessmentRecord).filter(
                StudentAssessmentRecord.assessment_id == ass.id,
                StudentAssessmentRecord.student_id == student.id
            ).first()
            
            obtained = record.obtained_marks if record and record.obtained_marks is not None else 0.0
            
            if ass.category.lower() == "exam":
                final_exam_obtained += obtained
            else:
                sessional_max += ass.max_marks
                sessional_obtained += obtained

        # --- NEW: STRICT NO-DATA CHECK ---
        # If the student has literally 0 attendance logs AND 0 sessional marks available, 
        # completely skip the AI prediction for this subject so it doesn't show fake baseline data.
        if len(logs) == 0 and sessional_max == 0.0:
            continue

        # 3. Scale Sessional to 50%
        scaled_sessional = (sessional_obtained / sessional_max * 50.0) if sessional_max > 0 else 0.0

        # 4. Predict Final Exam Score (out of 50) using Scikit-Learn
        predicted_final_exam = ai_engine.predict_grade(sessional_obtained, sessional_max, predicted_att_pct)

        # 5. Apply STRICT 50/50 Rules
        predicted_total = scaled_sessional + predicted_final_exam
        
        status = ResultStatus.PASS
        if scaled_sessional < 25.0:
            status = ResultStatus.FAIL # Auto-fail if sessional is dead
        elif predicted_final_exam < 25.0:
            status = ResultStatus.FAIL # Predicts they will fail the final
        elif predicted_total < 50.0 or predicted_att_pct < 75.0:
            status = ResultStatus.AT_RISK

        # 6. Save/Update PerformancePrediction database table
        try:
            pred_record = db.query(PerformancePrediction).filter(
                PerformancePrediction.student_id == student.id,
                PerformancePrediction.subject_id == subject.id
            ).first()

            if not pred_record:
                pred_record = PerformancePrediction(
                    student_id=student.id, subject_id=subject.id,
                    predicted_status=status, predicted_score=predicted_total,
                    confidence_score=0.85, model_version="v1.1-Strict5050"
                )
                db.add(pred_record)
            else:
                pred_record.predicted_status = status
                pred_record.predicted_score = predicted_total
            db.commit()
        except Exception as e:
            db.rollback()
            print(f"Database sync warning: {e}")

        # 7. Format for React
        predictions_data.append({
            "subject_code": subject.code,
            "subject_name": subject.name,
            "predicted_attendance": round(predicted_att_pct, 1),
            "predicted_score": round(predicted_total, 1),
            "sessional_score": round(scaled_sessional, 1),
            "final_exam_prediction": round(predicted_final_exam, 1),
            "status": status.value
        })

    return predictions_data