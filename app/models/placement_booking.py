"""
Dorismon V1.0 — Modelos SQLAlchemy 2.0 async.
Arquitectura: Course → Level → Module → Lesson
Roles: super_admin, teacher, student
"""
from __future__ import annotations
from datetime import datetime, date
from uuid import uuid4
from sqlalchemy import (
    String, ForeignKey, Numeric, Boolean, DateTime, Integer, Date, Text,
    func, Index, UniqueConstraint, JSON,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
import enum


class Base(DeclarativeBase):
    pass


def _uuid() -> str:
    return str(uuid4())


class UserRole(str, enum.Enum):
    super_admin = "super_admin"
    teacher = "teacher"
    student = "student"


class Modality(str, enum.Enum):
    online = "online"
    presencial = "presencial"
    hibrida = "hibrida"


class SessionStatus(str, enum.Enum):
    scheduled = "scheduled"
    completed = "completed"
    cancelled = "cancelled"


class AttendanceState(str, enum.Enum):
    present = "present"
    absent = "absent"
    late = "late"
    excused = "excused"


class QuestionType(str, enum.Enum):
    multiple_choice = "multiple_choice"
    true_false = "true_false"
    fill_blank = "fill_blank"
    short_answer = "short_answer"


class MaterialType(str, enum.Enum):
    pdf = "pdf"
    video = "video"
    audio = "audio"
    document = "document"
    image = "image"
    link = "link"


class PaymentStatus(str, enum.Enum):
    pending = "pending"
    paid = "paid"
    failed = "failed"
    refunded = "refunded"


class NotificationType(str, enum.Enum):
    new_assignment = "new_assignment"
    new_quiz = "new_quiz"
    grade_published = "grade_published"
    class_scheduled = "class_scheduled"
    reminder = "reminder"
    info = "info"


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String)
    full_name: Mapped[str] = mapped_column(String)
    phone: Mapped[str | None] = mapped_column(String, nullable=True)
    role: Mapped[UserRole] = mapped_column(default=UserRole.student)
    avatar_url: Mapped[str | None] = mapped_column(String, nullable=True)
    gender: Mapped[str | None] = mapped_column(String, nullable=True)  # V1.6.4: 'male', 'female', 'other', NULL
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    timezone: Mapped[str] = mapped_column(String, default="America/Santo_Domingo")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Student(Base):
    __tablename__ = "students"
    user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    current_level_id: Mapped[int | None] = mapped_column(ForeignKey("levels.id"), nullable=True)
    placement_done: Mapped[bool] = mapped_column(Boolean, default=False)
    grammar_score: Mapped[float | None] = mapped_column(Numeric(5, 2), nullable=True)
    speaking_score: Mapped[float | None] = mapped_column(Numeric(5, 2), nullable=True)
    listening_score: Mapped[float | None] = mapped_column(Numeric(5, 2), nullable=True)
    reading_score: Mapped[float | None] = mapped_column(Numeric(5, 2), nullable=True)
    writing_score: Mapped[float | None] = mapped_column(Numeric(5, 2), nullable=True)
    enrolled_at: Mapped[date] = mapped_column(Date, default=date.today)
    birth_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    address: Mapped[str | None] = mapped_column(Text, nullable=True)
    emergency_contact: Mapped[str | None] = mapped_column(String, nullable=True)
    is_paused: Mapped[bool] = mapped_column(Boolean, default=False)
    paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    pause_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    streak_days: Mapped[int] = mapped_column(Integer, default=0)
    last_activity_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Teacher(Base):
    __tablename__ = "teachers"
    user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    bio: Mapped[str | None] = mapped_column(Text, nullable=True)
    specialties: Mapped[str] = mapped_column(String, default="")
    modalities: Mapped[str] = mapped_column(String, default="online")
    levels_taught: Mapped[str | None] = mapped_column(String, nullable=True)  # V1.5.1: "A1,A2,B1" — niveles que enseña
    hire_date: Mapped[date] = mapped_column(Date, default=date.today)


class Course(Base):
    __tablename__ = "courses"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String, unique=True)
    name: Mapped[str] = mapped_column(String)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    image_url: Mapped[str | None] = mapped_column(String, nullable=True)
    color: Mapped[str] = mapped_column(String, default="#4361ee")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    order_index: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Level(Base):
    __tablename__ = "levels"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id", ondelete="CASCADE"))
    code: Mapped[str] = mapped_column(String)
    name: Mapped[str] = mapped_column(String)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    hours_required: Mapped[int] = mapped_column(Integer, default=120)
    order_index: Mapped[int] = mapped_column(Integer, default=0)
    __table_args__ = (UniqueConstraint("course_id", "code"),)


class Module(Base):
    __tablename__ = "modules"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    level_id: Mapped[int] = mapped_column(ForeignKey("levels.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    order_index: Mapped[int] = mapped_column(Integer, default=0)


class Lesson(Base):
    __tablename__ = "lessons"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    module_id: Mapped[int] = mapped_column(ForeignKey("modules.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    objectives: Mapped[str | None] = mapped_column(Text, nullable=True)
    can_do: Mapped[str | None] = mapped_column(String, nullable=True)
    video_url: Mapped[str | None] = mapped_column(String, nullable=True)
    pdf_url: Mapped[str | None] = mapped_column(String, nullable=True)
    audio_url: Mapped[str | None] = mapped_column(String, nullable=True)
    duration_min: Mapped[int] = mapped_column(Integer, default=15)
    order_index: Mapped[int] = mapped_column(Integer, default=0)
    is_published: Mapped[bool] = mapped_column(Boolean, default=True)


class LessonProgress(Base):
    __tablename__ = "lesson_progress"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    student_id: Mapped[str] = mapped_column(ForeignKey("students.user_id", ondelete="CASCADE"))
    lesson_id: Mapped[int] = mapped_column(ForeignKey("lessons.id", ondelete="CASCADE"))
    is_completed: Mapped[bool] = mapped_column(Boolean, default=False)
    progress_pct: Mapped[int] = mapped_column(Integer, default=0)
    last_viewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (UniqueConstraint("student_id", "lesson_id"),)


class Enrollment(Base):
    __tablename__ = "enrollments"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    student_id: Mapped[str] = mapped_column(ForeignKey("students.user_id", ondelete="CASCADE"))
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id"))
    level_id: Mapped[int] = mapped_column(ForeignKey("levels.id"))
    teacher_id: Mapped[str | None] = mapped_column(ForeignKey("teachers.user_id"), nullable=True)
    plan_id: Mapped[int | None] = mapped_column(ForeignKey("plans.id"), nullable=True)
    enrolled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    final_grade: Mapped[float | None] = mapped_column(Numeric(5, 2), nullable=True)


class Branch(Base):
    __tablename__ = "branches"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String, unique=True)
    address: Mapped[str | None] = mapped_column(Text, nullable=True)
    phone: Mapped[str | None] = mapped_column(String, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class Classroom(Base):
    __tablename__ = "classrooms"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    branch_id: Mapped[int] = mapped_column(ForeignKey("branches.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String)
    capacity: Mapped[int] = mapped_column(Integer, default=15)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class ClassSession(Base):
    __tablename__ = "class_sessions"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id"))
    level_id: Mapped[int] = mapped_column(ForeignKey("levels.id"))
    teacher_id: Mapped[str] = mapped_column(ForeignKey("teachers.user_id"))
    title: Mapped[str] = mapped_column(String)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    modality: Mapped[Modality] = mapped_column()
    starts_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    meeting_url: Mapped[str | None] = mapped_column(String, nullable=True)
    branch_id: Mapped[int | None] = mapped_column(ForeignKey("branches.id"), nullable=True)
    classroom_id: Mapped[int | None] = mapped_column(ForeignKey("classrooms.id"), nullable=True)
    capacity: Mapped[int] = mapped_column(Integer, default=15)
    recording_url: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[SessionStatus] = mapped_column(default=SessionStatus.scheduled)
    is_open_event: Mapped[bool] = mapped_column(Boolean, default=False)  # V1.2: evento abierto a cualquier estudiante
    teacher_notes: Mapped[str | None] = mapped_column(Text, nullable=True)  # V1.3 notas del profe post-clase
    module_id: Mapped[int | None] = mapped_column(ForeignKey("modules.id"), nullable=True)  # V1.3 vincular clase a módulo


class SessionAttendance(Base):
    __tablename__ = "session_attendance"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(ForeignKey("class_sessions.id", ondelete="CASCADE"))
    student_id: Mapped[str] = mapped_column(ForeignKey("students.user_id", ondelete="CASCADE"))
    state: Mapped[AttendanceState | None] = mapped_column(nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    recorded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (UniqueConstraint("session_id", "student_id"),)


class Quiz(Base):
    __tablename__ = "quizzes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lesson_id: Mapped[int | None] = mapped_column(ForeignKey("lessons.id"), nullable=True)
    teacher_id: Mapped[str] = mapped_column(ForeignKey("teachers.user_id"))
    title: Mapped[str] = mapped_column(String)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    level_id: Mapped[int | None] = mapped_column(ForeignKey("levels.id"), nullable=True)
    passing_score: Mapped[float] = mapped_column(Numeric(5, 2), default=60.0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    is_published: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class QuizQuestion(Base):
    __tablename__ = "quiz_questions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    quiz_id: Mapped[int] = mapped_column(ForeignKey("quizzes.id", ondelete="CASCADE"))
    type: Mapped[QuestionType] = mapped_column()
    statement: Mapped[str] = mapped_column(Text)
    options: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    correct_answer: Mapped[str] = mapped_column(Text)
    points: Mapped[float] = mapped_column(Numeric(5, 2), default=10.0)
    order_index: Mapped[int] = mapped_column(Integer, default=0)


class QuizAttempt(Base):
    __tablename__ = "quiz_attempts"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    quiz_id: Mapped[int] = mapped_column(ForeignKey("quizzes.id", ondelete="CASCADE"))
    student_id: Mapped[str] = mapped_column(ForeignKey("students.user_id", ondelete="CASCADE"))
    score: Mapped[float | None] = mapped_column(Numeric(5, 2), nullable=True)
    passed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class QuizAnswer(Base):
    __tablename__ = "quiz_answers"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    attempt_id: Mapped[str] = mapped_column(ForeignKey("quiz_attempts.id", ondelete="CASCADE"))
    question_id: Mapped[int] = mapped_column(ForeignKey("quiz_questions.id", ondelete="CASCADE"))
    answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_correct: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    points_earned: Mapped[float] = mapped_column(Numeric(5, 2), default=0.0)


class Assignment(Base):
    __tablename__ = "assignments"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    teacher_id: Mapped[str] = mapped_column(ForeignKey("teachers.user_id"))
    lesson_id: Mapped[int | None] = mapped_column(ForeignKey("lessons.id"), nullable=True)
    level_id: Mapped[int | None] = mapped_column(ForeignKey("levels.id"), nullable=True)
    title: Mapped[str] = mapped_column(String)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    instructions: Mapped[str | None] = mapped_column(Text, nullable=True)
    max_score: Mapped[float] = mapped_column(Numeric(5, 2), default=100.0)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    allow_file_upload: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AssignmentSubmission(Base):
    __tablename__ = "assignment_submissions"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    assignment_id: Mapped[int] = mapped_column(ForeignKey("assignments.id", ondelete="CASCADE"))
    student_id: Mapped[str] = mapped_column(ForeignKey("students.user_id", ondelete="CASCADE"))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_url: Mapped[str | None] = mapped_column(String, nullable=True)
    file_name: Mapped[str | None] = mapped_column(String, nullable=True)
    score: Mapped[float | None] = mapped_column(Numeric(5, 2), nullable=True)
    feedback: Mapped[str | None] = mapped_column(Text, nullable=True)
    graded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Material(Base):
    __tablename__ = "materials"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    type: Mapped[MaterialType] = mapped_column()
    url: Mapped[str] = mapped_column(String)
    course_id: Mapped[int | None] = mapped_column(ForeignKey("courses.id"), nullable=True)
    level_id: Mapped[int | None] = mapped_column(ForeignKey("levels.id"), nullable=True)
    module_id: Mapped[int | None] = mapped_column(ForeignKey("modules.id"), nullable=True)
    lesson_id: Mapped[int | None] = mapped_column(ForeignKey("lessons.id"), nullable=True)
    uploaded_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    is_public: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Observation(Base):
    __tablename__ = "observations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    student_id: Mapped[str] = mapped_column(ForeignKey("students.user_id", ondelete="CASCADE"))
    teacher_id: Mapped[str] = mapped_column(ForeignKey("teachers.user_id"))
    content: Mapped[str] = mapped_column(Text)
    is_private: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Notification(Base):
    __tablename__ = "notifications"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    type: Mapped[NotificationType] = mapped_column()
    title: Mapped[str] = mapped_column(String)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    link: Mapped[str | None] = mapped_column(String, nullable=True)
    is_read: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class Certificate(Base):
    __tablename__ = "certificates"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    code: Mapped[str] = mapped_column(String, unique=True, index=True)
    student_id: Mapped[str] = mapped_column(ForeignKey("students.user_id"))
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id"))
    level_id: Mapped[int] = mapped_column(ForeignKey("levels.id"))
    hours: Mapped[int] = mapped_column(Integer, default=120)
    final_grade: Mapped[float | None] = mapped_column(Numeric(5, 2), nullable=True)
    issued_at: Mapped[date] = mapped_column(Date, default=date.today)
    pdf_url: Mapped[str | None] = mapped_column(String, nullable=True)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)


class Plan(Base):
    __tablename__ = "plans"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String, unique=True)
    name: Mapped[str] = mapped_column(String)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    price: Mapped[float] = mapped_column(Numeric(10, 2))
    currency: Mapped[str] = mapped_column(String, default="USD")
    duration_months: Mapped[int] = mapped_column(Integer, default=1)
    features: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class Payment(Base):
    __tablename__ = "payments"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    student_id: Mapped[str] = mapped_column(ForeignKey("students.user_id"))
    plan_id: Mapped[int | None] = mapped_column(ForeignKey("plans.id"), nullable=True)
    amount: Mapped[float] = mapped_column(Numeric(10, 2))
    currency: Mapped[str] = mapped_column(String, default="USD")
    status: Mapped[PaymentStatus] = mapped_column(default=PaymentStatus.pending)
    method: Mapped[str | None] = mapped_column(String, nullable=True)
    reference: Mapped[str | None] = mapped_column(String, nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PlacementTest(Base):
    __tablename__ = "placement_tests"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    student_id: Mapped[str] = mapped_column(ForeignKey("students.user_id", ondelete="CASCADE"))
    grammar_score: Mapped[float | None] = mapped_column(Numeric(5, 2), nullable=True)
    reading_score: Mapped[float | None] = mapped_column(Numeric(5, 2), nullable=True)
    listening_score: Mapped[float | None] = mapped_column(Numeric(5, 2), nullable=True)
    writing_score: Mapped[float | None] = mapped_column(Numeric(5, 2), nullable=True)
    speaking_score: Mapped[float | None] = mapped_column(Numeric(5, 2), nullable=True)
    suggested_level_id: Mapped[int | None] = mapped_column(ForeignKey("levels.id"), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    user_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    action: Mapped[str] = mapped_column(String, index=True)
    module: Mapped[str] = mapped_column(String, index=True)
    target_id: Mapped[str | None] = mapped_column(String, nullable=True)
    ip: Mapped[str | None] = mapped_column(String, nullable=True)
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class InstituteSetting(Base):
    __tablename__ = "institute_settings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    name: Mapped[str] = mapped_column(String, default="Dorismon Language Institute")
    logo_url: Mapped[str | None] = mapped_column(String, nullable=True)
    primary_color: Mapped[str] = mapped_column(String, default="#4361ee")
    accent_color: Mapped[str] = mapped_column(String, default="#f4622a")
    contact_email: Mapped[str | None] = mapped_column(String, nullable=True)
    contact_phone: Mapped[str | None] = mapped_column(String, nullable=True)
    address: Mapped[str | None] = mapped_column(String, nullable=True)
    timezone: Mapped[str] = mapped_column(String, default="America/Santo_Domingo")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


Index("ix_sessions_starts", ClassSession.starts_at_utc)
Index("ix_attendance_session", SessionAttendance.session_id)
Index("ix_progress_student", LessonProgress.student_id)


# ============= PLACEMENT TEST V2 =============
class PlacementQuestion(Base):
    """Preguntas del placement test. Diseñadas para expansión:
    - difficulty_level y skill permiten test adaptativo futuro
    - audio_url e image_url permiten listening/visual en futuro
    - is_active permite curaduría sin borrar
    """
    __tablename__ = "placement_questions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    statement: Mapped[str] = mapped_column(Text)
    option_a: Mapped[str] = mapped_column(String)
    option_b: Mapped[str] = mapped_column(String)
    option_c: Mapped[str] = mapped_column(String)
    option_d: Mapped[str] = mapped_column(String)
    correct_option: Mapped[str] = mapped_column(String)  # "a", "b", "c", "d"
    difficulty_level: Mapped[str] = mapped_column(String)  # "A1","A2","B1","B2","C1"
    skill: Mapped[str] = mapped_column(String, default="grammar")  # grammar/vocabulary/reading/listening
    audio_url: Mapped[str | None] = mapped_column(String, nullable=True)  # preparado para listening
    image_url: Mapped[str | None] = mapped_column(String, nullable=True)  # preparado para visual
    explanation: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    order_index: Mapped[int] = mapped_column(Integer, default=0)


class PlacementAnswer(Base):
    """Cada respuesta del estudiante en su placement test.
    Permite reconstruir el test y análisis detallado.
    """
    __tablename__ = "placement_answers"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    placement_test_id: Mapped[str] = mapped_column(ForeignKey("placement_tests.id", ondelete="CASCADE"))
    question_id: Mapped[int] = mapped_column(ForeignKey("placement_questions.id"))
    selected_option: Mapped[str | None] = mapped_column(String, nullable=True)
    is_correct: Mapped[bool] = mapped_column(Boolean, default=False)
    answered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SpeakingRecording(Base):
    """Preparada para V2 con IA tipo Whisper.
    Por ahora vacía, solo estructura."""
    __tablename__ = "speaking_recordings"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    student_id: Mapped[str] = mapped_column(ForeignKey("students.user_id", ondelete="CASCADE"))
    prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    audio_url: Mapped[str | None] = mapped_column(String, nullable=True)
    transcription: Mapped[str | None] = mapped_column(Text, nullable=True)
    fluency_score: Mapped[float | None] = mapped_column(Numeric(5, 2), nullable=True)
    pronunciation_score: Mapped[float | None] = mapped_column(Numeric(5, 2), nullable=True)
    accuracy_score: Mapped[float | None] = mapped_column(Numeric(5, 2), nullable=True)
    evaluated_by_ai: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ============= EVENTOS ABIERTOS V1.2 =============
class EventRegistration(Base):
    """Registro de estudiante a un evento abierto (clase no-regular)."""
    __tablename__ = "event_registrations"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(ForeignKey("class_sessions.id", ondelete="CASCADE"))
    student_id: Mapped[str] = mapped_column(ForeignKey("students.user_id", ondelete="CASCADE"))
    registered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (UniqueConstraint("session_id", "student_id"),)



# ============= V1.3 — Progress tracking =============
class ModuleProgress(Base):
    """Progreso del estudiante en un módulo (módulos completados/en progreso)."""
    __tablename__ = "module_progress"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    student_id: Mapped[str] = mapped_column(ForeignKey("students.user_id", ondelete="CASCADE"))
    module_id: Mapped[int] = mapped_column(ForeignKey("modules.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String, default="locked")  # locked, in_progress, completed
    attended_count: Mapped[int] = mapped_column(Integer, default=0)
    quiz_passed: Mapped[bool] = mapped_column(Boolean, default=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (UniqueConstraint("student_id", "module_id"),)


class PlanFeature(Base):
    """Features editables de un plan (mientras más features, más caro)."""
    __tablename__ = "plan_features"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("plans.id", ondelete="CASCADE"))
    feature: Mapped[str] = mapped_column(String)
    is_included: Mapped[bool] = mapped_column(Boolean, default=True)
    order_index: Mapped[int] = mapped_column(Integer, default=0)
