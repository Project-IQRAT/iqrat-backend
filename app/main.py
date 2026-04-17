from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from app.api.v1.endpoints import system
from app.core.config import settings
from app.db.base import Base
import os

# ==========================================
# NEW: CLOUDINARY CONFIGURATION
# ==========================================
import cloudinary
from cloudinary import uploader

cloudinary.config( 
    cloud_name="dx7qvijds", 
    api_key="565849931171524", 
    api_secret="IrOvlkFbGp0VRlmeT3Hg7SDEfIs",
    secure=True
)

# Import all routers (Notice attendance is gone)
from app.api.v1.endpoints import auth, users, academic

app = FastAPI(title=settings.PROJECT_NAME)

# ==========================================
# 1. CORS MIDDLEWARE (The Bridge to React) 
# ==========================================
# 1. Get the frontend URL from environment variables
live_frontend = os.getenv("FRONTEND_URL")

# 2. Start with your known local and production URLs
origins = [
    "http://localhost:5173", 
    "http://localhost:3000", 
    "http://127.0.0.1:5173", 
    "http://127.0.0.1:3000",
    "https://iqrat.vercel.app",
]

# 3. Add the live_frontend to the list only if it's actually set in Render
if live_frontend:
    # Ensure no trailing slash which causes CORS to fail
    origins.append(live_frontend.rstrip("/"))

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins, 
    allow_credentials=True,
    allow_methods=["*"], 
    allow_headers=["*"], 
)

# ==========================================
# 2. MOUNT STATIC FILES (For Student Photos) 
# ==========================================
# Create the static directory if it doesn't exist to prevent a startup crash
# (We keep this so any existing code that relies on /static doesn't break)
if not os.path.exists("static"):
    os.makedirs("static")

app.mount("/static", StaticFiles(directory="static"), name="static")

# ==========================================
# 3. REGISTER ROUTERS (The API Map) 
# ==========================================
app.include_router(auth.router, prefix="/api/v1/auth", tags=["Authentication"])
app.include_router(users.router, prefix="/api/v1/users", tags=["Onboarding"])
app.include_router(academic.router, prefix="/api/v1/academic", tags=["Academic Structure"])
app.include_router(system.router, prefix="/api/v1/system", tags=["system"])

@app.get("/")
def read_root():
    return {"message": "Welcome to IQRAT Backend API"}