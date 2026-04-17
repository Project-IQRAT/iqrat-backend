import os
import random
import re
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, status, Form
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session
from sqlalchemy import or_
from app.db.session import SessionLocal
from app.core.security import verify_password, create_access_token, get_password_hash
from app.models.users import User, Student, Lecturer, Admin 

router = APIRouter()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

@router.post("/login")
def login_for_access_token(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    # 1. Find user by Email OR Student Registration Number (reg_no)
    user = db.query(User).outerjoin(Student, User.id == Student.user_id).filter(
        or_(
            User.email == form_data.username,
            Student.reg_no == form_data.username
        )
    ).first()
    
    # 2. Check Password
    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email/roll number or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
        
    # --- NEW: INTERCEPT FIRST LOGIN ---
    if user.requires_password_change:
        return {
            "status": "password_change_required", 
            "username": form_data.username,
            "temp_password": form_data.password
        }
    
    # 3. Extract Real Details to put inside the Token!
    role_str = user.role.value if hasattr(user.role, 'value') else user.role
    
    full_name = "User"
    roll_no = form_data.username
    photo_path = ""
    extra_claims = {}  # <-- DEFINED HERE IN THE MAIN SCOPE
    
    # Fetch specific profile based on role
    if str(role_str).lower() == "student":
        profile = db.query(Student).filter(Student.user_id == user.id).first()
        if profile:
            full_name = profile.full_name
            roll_no = profile.reg_no
            photo_path = f"/{profile.photo_path}" if profile.photo_path else ""
            
    elif str(role_str).lower() == "lecturer":
        profile = db.query(Lecturer).filter(Lecturer.user_id == user.id).first()
        if profile:
            full_name = profile.full_name
            
    elif str(role_str).lower() == "admin":
        profile = db.query(Admin).filter(Admin.user_id == user.id).first()
        if profile:
            full_name = profile.full_name
            # --- NEW: Inject Admin Security Scopes into Token ---
            r_level = profile.role_level.value if hasattr(profile.role_level, 'value') else profile.role_level
            extra_claims["role_level"] = str(r_level).lower()
            extra_claims["permissions"] = profile.permissions
            extra_claims["department_id"] = profile.department_id

    # 4. Create JWT with the extra profile data inside it
    token_data = {
        "sub": user.email, 
        "role": role_str,
        "name": full_name,
        "roll": roll_no,
        "photo": photo_path,
        **extra_claims # Merges the admin claims securely
    }
    
    access_token = create_access_token(data=token_data)
    
    return {"access_token": access_token, "token_type": "bearer"}

# Temporary in-memory store for OTPs (In production, use Redis or a DB table)
# Format: { "username": {"otp": "123456", "expires_at": datetime} }
OTP_STORE = {}

@router.post("/forgot-password/request-otp")
def request_otp(
    username: str = Form(...),
    contact: str = Form(...),
    db: Session = Depends(get_db)
):
    """Generates a 6-digit OTP if the username and contact match."""
    # 1. Find the user based on the generic username (email, roll no, emp code, admin id)
    user = db.query(User).filter(User.email == username).first()
    
    # If not found by email, check profiles
    if not user:
        student = db.query(Student).filter(Student.reg_no == username).first()
        if student: user = db.query(User).filter(User.id == student.user_id).first()
        
    if not user:
        lecturer = db.query(Lecturer).filter(Lecturer.employee_code == username).first()
        if lecturer: user = db.query(User).filter(User.id == lecturer.user_id).first()
            
    if not user:
        admin = db.query(Admin).filter(Admin.admin_id == username).first()
        if admin: user = db.query(User).filter(User.id == admin.user_id).first()

    if not user:
        raise HTTPException(status_code=404, detail="Username not found.")

    # 2. Generate a 6-digit OTP
    otp_code = str(random.randint(100000, 999999))
    
    # 3. Store the OTP with a 10-minute expiration
    OTP_STORE[username] = {
        "otp": otp_code,
        "expires_at": datetime.now() + timedelta(minutes=10)
    }

    # 4. SEND ACTUAL EMAIL VIA GMAIL SMTP
    # Keep these in your Render Environment Variables in production!
    sender_email = os.getenv("SMTP_EMAIL", "project.iqrat@gmail.com") 
    sender_password = os.getenv("SMTP_PASSWORD", "wpdq dbib nqow eflc") 

    try:
        msg = MIMEMultipart()
        msg["Subject"] = "IQRAT Security: Password Reset OTP"
        msg["From"] = f"IQRAT System <{sender_email}>"
        msg["To"] = user.email # ALWAYS send to the DB registered email, never trust the frontend!

        body = f"""
        Hello,

        A password reset was requested for your IQRAT account ({username}).
        Your 6-digit OTP code is: 

        {otp_code}

        This code is valid for 10 minutes. If you did not request this, please ignore this email or contact your administrator immediately.

        Securely,
        IQRAT Automated System
        """
        msg.attach(MIMEText(body, "plain"))

        # Connect to Gmail and send!
        server = smtplib.SMTP("smtp.gmail.com", 587)
        server.starttls()
        server.login(sender_email, sender_password)
        server.send_message(msg)
        server.quit()
        
        print(f"OTP successfully emailed to {user.email}")
        
    except Exception as e:
        print(f"Email sending failed: {e}")
        raise HTTPException(
            status_code=500, 
            detail="Failed to send email. Please contact the administrator."
        )

    # Mask the email for privacy on the frontend (e.g. j***@gmail.com)
    masked_email = f"{user.email[0]}***@{user.email.split('@')[1]}"
    return {"msg": f"A 6-digit OTP has been sent securely to {masked_email}"}


@router.post("/forgot-password/verify-otp")
def verify_otp(
    username: str = Form(...),
    otp: str = Form(...),
):
    """Verifies the OTP entered by the user."""
    record = OTP_STORE.get(username)
    
    if not record:
        raise HTTPException(status_code=400, detail="No OTP requested for this user.")
        
    if datetime.now() > record["expires_at"]:
        del OTP_STORE[username]
        raise HTTPException(status_code=400, detail="OTP has expired. Please request a new one.")
        
    if record["otp"] != otp:
        raise HTTPException(status_code=400, detail="Invalid OTP code.")
        
    return {"msg": "OTP Verified! You may now reset your password."}


@router.post("/forgot-password/reset")
def reset_password(
    username: str = Form(...),
    otp: str = Form(...),
    new_password: str = Form(...),
    db: Session = Depends(get_db)
):
    """Finalizes the password reset process."""
    # 1. Final verification of OTP to ensure security
    record = OTP_STORE.get(username)
    if not record or record["otp"] != otp:
        raise HTTPException(status_code=403, detail="Unauthorized reset attempt.")

    # 2. Find the user again
    user = db.query(User).filter(User.email == username).first()
    if not user:
        student = db.query(Student).filter(Student.reg_no == username).first()
        if student: user = db.query(User).filter(User.id == student.user_id).first()
    if not user:
        lecturer = db.query(Lecturer).filter(Lecturer.employee_code == username).first()
        if lecturer: user = db.query(User).filter(User.id == lecturer.user_id).first()
    if not user:
        admin = db.query(Admin).filter(Admin.admin_id == username).first()
        if admin: user = db.query(User).filter(User.id == admin.user_id).first()

    # 3. Hash the new password and update the database
    user.hashed_password = get_password_hash(new_password)
    db.commit()
    
    # 4. Clear the OTP from memory so it can't be reused
    del OTP_STORE[username]
    
    return {"msg": "Password reset successfully! You can now log in."}

@router.post("/force-password-change")
def force_password_change(
    username: str = Form(...),
    current_password: str = Form(...),
    new_password: str = Form(...),
    db: Session = Depends(get_db)
):
    """Handles the mandatory password change on first login."""
    user = db.query(User).outerjoin(Student, User.id == Student.user_id).filter(
        or_(User.email == username, Student.reg_no == username)
    ).first()

    if not user or not verify_password(current_password, user.hashed_password):
        raise HTTPException(status_code=403, detail="Invalid credentials.")

    # STRICT COMPLEXITY CHECK
    # Min 8 chars, 1 uppercase, 1 lowercase, 1 number, 1 special character
    password_regex = r"^(?=.*[a-z])(?=.*[A-Z])(?=.*\d)(?=.*[@$!%*?&])[A-Za-z\d@$!%*?&]{8,}$"
    if not re.match(password_regex, new_password):
        raise HTTPException(
            status_code=400, 
            detail="Password must be at least 8 characters long and include an uppercase letter, a lowercase letter, a number, and a special symbol (@$!%*?&)."
        )

    # Update password and clear the flag
    user.hashed_password = get_password_hash(new_password)
    user.requires_password_change = False
    db.commit()

    return {"msg": "Password updated successfully. You can now log in."}