from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from app.api.v1.endpoints import system
from app.core.config import settings
from app.db.base import Base
import os

# Import all routers (Notice attendance is gone)
from app.api.v1.endpoints import auth, users, academic

app = FastAPI(title=settings.PROJECT_NAME)

# ==========================================
# 1. CORS MIDDLEWARE (The Bridge to React) 
# ==========================================
# We grab the live Vercel URL from Render's environment, otherwise we use a placeholder
live_frontend = os.getenv("FRONTEND_URL", "https://iqrat-temp-url.vercel.app")

origins = [
    "http://localhost:5173", 
    "http://localhost:3000", 
    "http://127.0.0.1:5173", 
    "http://127.0.0.1:3000",
    live_frontend, # <--- This allows your Vercel app to connect!
    "https://iqrat-frontend.vercel.app"
]

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
app.mount("/static", StaticFiles(directory="static"), name="static")

# ==========================================
# 3. REGISTER ROUTERS (The API Map) 
# ==========================================
app.include_router(auth.router, prefix="/api/v1/auth", tags=["Authentication"])
app.include_router(users.router, prefix="/api/v1/users", tags=["Onboarding"])
app.include_router(academic.router, prefix="/api/v1/academic", tags=["Academic Structure"])
app.include_router(system.router, prefix="/api/v1/system", tags=["system"])

# --- REMOVED THE ATTENDANCE ROUTER LINE FROM HERE ---

@app.get("/")
def read_root():
    return {"message": "Welcome to IQRAT Backend API"}