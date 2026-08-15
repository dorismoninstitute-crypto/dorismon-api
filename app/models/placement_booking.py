"""
Dorismon V1.0 — Modelos SQLAlchemy 2.0 async.
Arquitectura: Course → Level → Module → Lesson
Roles: super_admin, teacher, student
"""
from __future__ import annotations
from datetime import datetime, date
from uuid import uuid4
from sqlalchemy import (
    String, ForeignKey, Numeric, Boolean, DateTime, Integer, Date, Text, Float,
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
    # V2.9
    class_cancelled = "class_cancelled"
    class_reminder_24h = "class_reminder_24h"
    general = "general"


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
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False)  # V2.1: verificación email real
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
    # V3.9: Perfil de estudiante archivado (ej: convertido a profesor). No se borra,
    # solo se marca inactivo para que no aparezca en listas/reportes de estudiantes.
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    archived_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    streak_days: Mapped[int] = mapped_column(Integer, default=0)
    last_activity_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # V2.2: Datos personales adicionales
    document_type: Mapped[str | None] = mapped_column(String, nullable=True)  # cedula/pasaporte/otro
    document_number: Mapped[str | None] = mapped_column(String, nullable=True)
    city: Mapped[str | None] = mapped_column(String, nullable=True)
    sector: Mapped[str | None] = mapped_column(String, nullable=True)
    nationality: Mapped[str | None] = mapped_column(String, nullable=True, default="Dominicana")

    # V2.2: Contacto de emergencia detallado
    emergency_contact_name: Mapped[str | None] = mapped_column(String, nullable=True)
    emergency_contact_relationship: Mapped[str | None] = mapped_column(String, nullable=True)  # padre/madre/etc
    emergency_contact_phone: Mapped[str | None] = mapped_column(String, nullable=True)

    # V2.2: Tutor (obligatorio si menor de edad)
    tutor_name: Mapped[str | None] = mapped_column(String, nullable=True)
    tutor_relationship: Mapped[str | None] = mapped_column(String, nullable=True)
    tutor_document: Mapped[str | None] = mapped_column(String, nullable=True)
    tutor_phone: Mapped[str | None] = mapped_column(String, nullable=True)
    tutor_email: Mapped[str | None] = mapped_column(String, nullable=True)

    # V2.2: Información adicional
    how_found_us: Mapped[str | None] = mapped_column(String, nullable=True)  # google/facebook/referred/etc
    referred_by: Mapped[str | None] = mapped_column(String, nullable=True)
    special_notes: Mapped[str | None] = mapped_column(Text, nullable=True)  # alergias, condiciones, etc.


class Teacher(Base):
    __tablename__ = "teachers"
    user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    bio: Mapped[str | None] = mapped_column(Text, nullable=True)
    specialties: Mapped[str] = mapped_column(String, default="")
    modalities: Mapped[str] = mapped_column(String, default="online")
    levels_taught: Mapped[str | None] = mapped_column(String, nullable=True)  # V1.5.1
    hire_date: Mapped[date] = mapped_column(Date, default=date.today)
    # V1.9: Tarifas de pago por tipo de clase (en moneda local, ej: RD$)
    rate_group: Mapped[float] = mapped_column(Float, default=500.0)  # clase grupal regular
    rate_private: Mapped[float] = mapped_column(Float, default=1000.0)  # clase privada 1-a-1
    rate_event: Mapped[float] = mapped_column(Float, default=750.0)  # evento/workshop


class TeacherPayment(Base):
    """V1.9: Registro de pagos a profesores por período mensual.

    Se crea cuando el admin marca como pagado un período.
    El cálculo de "lo que falta cobrar" se hace on-the-fly basado en clases con asistencia.

    V2.9.1: constraint único (teacher, año, mes) para evitar doble pago por doble click.
    """
    __tablename__ = "teacher_payments"
    __table_args__ = (
        UniqueConstraint("teacher_id", "period_year", "period_month",
                         name="uq_teacher_payment_period"),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    teacher_id: Mapped[str] = mapped_column(ForeignKey("teachers.user_id"))
    period_year: Mapped[int] = mapped_column(Integer)  # 2026
    period_month: Mapped[int] = mapped_column(Integer)  # 1-12
    classes_count: Mapped[int] = mapped_column(Integer)  # snapshot al momento de pagar
    group_count: Mapped[int] = mapped_column(Integer, default=0)
    private_count: Mapped[int] = mapped_column(Integer, default=0)
    event_count: Mapped[int] = mapped_column(Integer, default=0)
    total_amount: Mapped[float] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String, default="DOP")  # peso dominicano
    payment_method: Mapped[str | None] = mapped_column(String, nullable=True)  # transferencia/efectivo/etc
    reference: Mapped[str | None] = mapped_column(String, nullable=True)  # # de transferencia
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    paid_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    paid_by_admin_id: Mapped[str] = mapped_column(ForeignKey("users.id"))


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
    # V3.9.33 — A qué GRUPO pertenece este estudiante.
    #
    # EL PROBLEMA QUE RESUELVE: antes el estudiante veía TODAS las clases de
    # su nivel. Si tenías dos grupos de B1 (mañana y noche), todos los B1
    # veían los dos horarios y no había forma de decir "María va al de la
    # mañana".
    #
    # La serie de clases ES el grupo. Si queda vacío, el estudiante sigue
    # viendo todo su nivel (así no se rompe nada de lo que ya existe).
    series_id: Mapped[str | None] = mapped_column(ForeignKey("class_series.id"), nullable=True)
    # V2.3: Modalidad de inscripción (online/presencial/hibrida)
    modality: Mapped[Modality] = mapped_column(default=Modality.online)
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
    # V1.7: Serie y clase privada
    series_id: Mapped[str | None] = mapped_column(ForeignKey("class_series.id", ondelete="SET NULL"), nullable=True)  # V1.7: pertenece a una serie
    student_id: Mapped[str | None] = mapped_column(ForeignKey("students.user_id"), nullable=True)  # V1.7: si está seteado = clase privada 1-a-1
    # V3.9.26: dónde ocurre el video de esta clase.
    #   "meet"     → el enlace de meeting_url (Google Meet, Zoom, el que sea)
    #   "dorismon" → sala propia dentro de dorismon.com (LiveKit)
    # Se guarda por clase para poder cambiar una sola sin tocar las demás,
    # y para tener plan B si el video propio falla en vivo.
    video_provider: Mapped[str] = mapped_column(String, default="meet", server_default="meet")
    counts_for_progress: Mapped[bool] = mapped_column(Boolean, default=True)  # V1.7: privadas pueden no contar para CEFR
    # V2.9: Recordatorios automáticos + cancelaciones del profe
    reminder_24h_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # V3.9.32: aviso de "tu clase empieza en 30 minutos" (para prepararse)
    reminder_30m_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # V3.9.43 — Histórico del profesor. Regla de Luis: sustituir NO debe
    # destruir el pasado. teacher_id sigue siendo "quien la da" (y quien
    # cobra), pero se guarda quién estaba programado originalmente.
    scheduled_teacher_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    # Motivo si la clase se canceló (p.ej. "cancelled_by_teacher")
    cancel_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    # V3.9.43 — Alerta de profesor que no inició la clase. NO cancela nada:
    # solo deja constancia para que Dirección decida.
    teacher_absent_alert_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancellation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    cancelled_by_user_id: Mapped[str | None] = mapped_column(String, nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ClassSeries(Base):
    """V1.7: Serie de clases recurrentes. Una serie genera N clases automáticamente."""
    __tablename__ = "class_series"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String)  # "B1 Nocturno"
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id"))
    level_id: Mapped[int] = mapped_column(ForeignKey("levels.id"))
    teacher_id: Mapped[str] = mapped_column(ForeignKey("teachers.user_id"))
    plan_id: Mapped[int | None] = mapped_column(ForeignKey("plans.id"), nullable=True)
    days_of_week: Mapped[str] = mapped_column(String)  # CSV: "mon,wed,fri" (0=mon...6=sun)
    start_time_hhmm: Mapped[str] = mapped_column(String)  # "19:00"
    duration_min: Mapped[int] = mapped_column(Integer, default=90)
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)  # opcional si num_classes está seteado
    num_classes: Mapped[int | None] = mapped_column(Integer, nullable=True)  # opcional si end_date está seteado
    modality: Mapped[Modality] = mapped_column()
    meeting_url: Mapped[str | None] = mapped_column(String, nullable=True)
    branch_id: Mapped[int | None] = mapped_column(ForeignKey("branches.id"), nullable=True)
    classroom_id: Mapped[int | None] = mapped_column(ForeignKey("classrooms.id"), nullable=True)
    module_rotation: Mapped[str | None] = mapped_column(String, nullable=True)  # CSV de module_ids para rotar
    capacity: Mapped[int] = mapped_column(Integer, default=15)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SessionAttendance(Base):
    __tablename__ = "session_attendance"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(ForeignKey("class_sessions.id", ondelete="CASCADE"))
    student_id: Mapped[str] = mapped_column(ForeignKey("students.user_id", ondelete="CASCADE"))
    state: Mapped[AttendanceState | None] = mapped_column(nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    recorded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (UniqueConstraint("session_id", "student_id"),)


class AbsenceNotice(Base):
    """V3.0: Aviso anticipado del estudiante de que faltará a una clase.

    Lo crea el estudiante ANTES de la clase. El profe lo ve al pasar asistencia.
    Distinto de SessionAttendance (que la marca el profe DESPUÉS).
    """
    __tablename__ = "absence_notices"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(ForeignKey("class_sessions.id", ondelete="CASCADE"))
    student_id: Mapped[str] = mapped_column(ForeignKey("students.user_id", ondelete="CASCADE"))
    reason: Mapped[str] = mapped_column(Text)
    # Si avisó con tiempo (>= 2h antes) o a último momento
    notified_in_advance: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (UniqueConstraint("session_id", "student_id", name="uq_absence_session_student"),)


class Quiz(Base):
    __tablename__ = "quizzes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lesson_id: Mapped[int | None] = mapped_column(ForeignKey("lessons.id"), nullable=True)
    teacher_id: Mapped[str] = mapped_column(ForeignKey("teachers.user_id"))
    title: Mapped[str] = mapped_column(String)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    level_id: Mapped[int | None] = mapped_column(ForeignKey("levels.id"), nullable=True)
    # V3.9.45 — A qué grupo va este quiz (mismo criterio que Assignment).
    # NULL = a todos los del profesor en ese nivel.
    series_id: Mapped[str | None] = mapped_column(ForeignKey("class_series.id"), nullable=True)
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


class AssignmentKind(str, enum.Enum):
    """V3.9.33 — Los tipos de tarea que puede poner el profesor.

    Antes solo existía "escribe tu respuesta". Para enseñar un idioma hace
    falta más: sobre todo hablar y escuchar, que es lo que decide el nivel real.
    """
    written = "written"          # ✍️ Escribe su respuesta (el que ya existía)
    file = "file"                # 📎 Sube foto de su hoja o un PDF
    audio = "audio"              # 🎤 Graba su voz — practicar pronunciación
    listening = "listening"      # 👂 Escucha un audio o video y responde
    fill_blanks = "fill_blanks"  # ✏️ Completa los espacios (se califica solo)
    link = "link"                # 🔗 Entrega un enlace
    check = "check"              # ✅ Solo marcar como hecha (lecturas, repaso)


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
    # V3.9.45 — A qué GRUPO va esta tarea.
    #
    # EL HUECO QUE CIERRA: antes la audiencia era teacher_id + level_id. Eso
    # separaba a dos profesores distintos, pero NO a dos grupos del MISMO
    # profesor: si Carlos daba B1 mañana y B1 noche, una tarea para uno le
    # llegaba a los dos.
    #
    # NULL = para todos los estudiantes de ese profesor en ese nivel (así se
    # comportan las tareas que ya existen: compatibilidad total).
    series_id: Mapped[str | None] = mapped_column(ForeignKey("class_series.id"), nullable=True)

    # V3.9.33 — Tipo de tarea y su contenido
    kind: Mapped[AssignmentKind] = mapped_column(default=AssignmentKind.written,
                                                 server_default="written")
    # Para "escuchar y responder": el audio o video que deben escuchar
    media_url: Mapped[str | None] = mapped_column(String, nullable=True)
    # Para "completar espacios": las frases y sus respuestas, en JSON
    blanks_json: Mapped[str | None] = mapped_column(Text, nullable=True)
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

    # V3.9.46 P1 — AUDIENCIA DEL MATERIAL.
    #
    # Los tres casos que pediste:
    #   INSTITUCIONAL → audience_kind="institutional" (curso/nivel/módulo/lección)
    #   DEL PROFESOR  → audience_kind="teacher", con series_id si es de un grupo
    #   INDIVIDUAL    → audience_kind="student", con student_id
    #
    # ⚠️ COMPATIBILIDAD: los materiales que YA EXISTEN quedan como
    # "institutional", que es exactamente como se comportaban (is_public +
    # filtro por nivel). NO se les inventa una audiencia que nadie definió.
    audience_kind: Mapped[str] = mapped_column(
        String, default="institutional", server_default="institutional")
    # Para material de un grupo concreto
    series_id: Mapped[str | None] = mapped_column(ForeignKey("class_series.id"), nullable=True)
    # Para material dirigido a un solo estudiante (feedback, refuerzo)
    student_id: Mapped[str | None] = mapped_column(ForeignKey("students.user_id"), nullable=True)
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
    # V3.9.28: por qué se anuló y cuándo. No se borra el certificado: queda
    # el registro de que existió y fue anulado (eso es lo correcto).
    revoked_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


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


# ============= V2.6 — SISTEMA DE PAGOS POR TRANSFERENCIA =============

class BankAccountType(str, enum.Enum):
    savings = "savings"      # Ahorros
    checking = "checking"    # Corriente


class BankAccount(Base):
    """V2.6: Cuentas bancarias del instituto donde los estudiantes hacen transferencias.

    Configurable por admin. Solo las activas se muestran al estudiante.
    """
    __tablename__ = "bank_accounts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bank_name: Mapped[str] = mapped_column(String)  # BHD, Popular, Banreservas, etc.
    account_type: Mapped[BankAccountType] = mapped_column(default=BankAccountType.savings)
    account_number: Mapped[str] = mapped_column(String)
    holder_name: Mapped[str] = mapped_column(String)  # Titular
    holder_document: Mapped[str] = mapped_column(String)  # Cédula o RNC
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)  # Instrucciones extra
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PaymentProofStatus(str, enum.Enum):
    pending = "pending"      # Esperando verificación
    approved = "approved"    # Admin aprobó → estudiante inscrito
    rejected = "rejected"    # Admin rechazó


class PaymentMethod(str, enum.Enum):
    bank_transfer = "bank_transfer"
    yappy = "yappy"
    tpago = "tpago"
    pingdigital = "pingdigital"
    cash = "cash"
    other = "other"


class PaymentProof(Base):
    """V2.6: Prueba de pago subida por el estudiante.

    Cuando es aprobada por admin, se crea la inscripción automáticamente.
    """
    __tablename__ = "payment_proofs"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    student_id: Mapped[str] = mapped_column(ForeignKey("students.user_id"))
    plan_id: Mapped[int] = mapped_column(ForeignKey("plans.id"))
    course_id: Mapped[int | None] = mapped_column(ForeignKey("courses.id"), nullable=True)
    level_id: Mapped[int | None] = mapped_column(ForeignKey("levels.id"), nullable=True)
    modality: Mapped[Modality] = mapped_column(default=Modality.online)

    # Datos del pago
    amount: Mapped[float] = mapped_column(Numeric(10, 2))
    currency: Mapped[str] = mapped_column(String, default="DOP")
    method: Mapped[PaymentMethod] = mapped_column(default=PaymentMethod.bank_transfer)
    bank_origin: Mapped[str | None] = mapped_column(String, nullable=True)  # Banco desde donde envió
    payment_date: Mapped[date] = mapped_column(Date)
    reference_number: Mapped[str] = mapped_column(String)  # Número de transacción
    voucher_url: Mapped[str] = mapped_column(Text)  # Base64 o URL de screenshot

    # Estado
    status: Mapped[PaymentProofStatus] = mapped_column(default=PaymentProofStatus.pending)
    student_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    admin_notes: Mapped[str | None] = mapped_column(Text, nullable=True)  # Motivo si rechaza
    reviewed_by_admin_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Si fue aprobado, qué inscripción se creó
    enrollment_id: Mapped[str | None] = mapped_column(ForeignKey("enrollments.id"), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TrialClass(Base):
    """V2.6: Clase de prueba GRATIS para nuevos estudiantes.

    Cada estudiante puede tener UNA sola clase de prueba.
    """
    __tablename__ = "trial_classes"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    student_id: Mapped[str] = mapped_column(ForeignKey("students.user_id"), unique=True)  # 1 por estudiante
    modality: Mapped[Modality] = mapped_column(default=Modality.online)
    preferred_level: Mapped[str | None] = mapped_column(String, nullable=True)  # A1, A2, B1, etc.
    preferred_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    preferred_time: Mapped[str | None] = mapped_column(String, nullable=True)  # "morning", "afternoon", "evening"
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Cuando admin la agenda
    session_id: Mapped[str | None] = mapped_column(ForeignKey("class_sessions.id"), nullable=True)
    teacher_id: Mapped[str | None] = mapped_column(ForeignKey("teachers.user_id"), nullable=True)
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Estado
    status: Mapped[str] = mapped_column(String, default="requested")  # requested, scheduled, completed, no_show, cancelled
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    student_feedback: Mapped[str | None] = mapped_column(Text, nullable=True)
    converted_to_paid: Mapped[bool] = mapped_column(Boolean, default=False)  # Si después se inscribió a un plan
    # V3.0.2: control de reagenda (el estudiante puede reagendar 1 vez si no asistió)
    reschedule_count: Mapped[int] = mapped_column(Integer, default=0)
    reschedule_requested: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


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


class SiteImage(Base):
    """V3.9.23 — Imágenes de la página pública, subibles desde el admin.

    Cada 'slot' es un espacio con nombre fijo en la landing (foto principal,
    captura de la plataforma, etc). El admin sube la imagen, se guarda en
    Cloudinary y aquí queda el enlace. Cambiar la foto NO requiere deploy.
    """
    __tablename__ = "site_images"
    slot: Mapped[str] = mapped_column(String, primary_key=True)
    url: Mapped[str] = mapped_column(String)
    public_id: Mapped[str | None] = mapped_column(String, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class Testimonial(Base):
    """V3.9.23 — Testimonios reales de estudiantes para la landing.

    La sección solo se muestra en la página cuando hay al menos uno activo:
    si está vacía, no aparece y el diseño no se rompe.
    """
    __tablename__ = "testimonials"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String)
    role: Mapped[str | None] = mapped_column(String, nullable=True)  # "Arquitecta", "Estudiante"
    text: Mapped[str] = mapped_column(Text)
    photo_url: Mapped[str | None] = mapped_column(String, nullable=True)
    photo_public_id: Mapped[str | None] = mapped_column(String, nullable=True)
    rating: Mapped[int] = mapped_column(Integer, default=5)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class VideoPresence(Base):
    """V3.9.27 — Quién estuvo en la videollamada de una clase y cuánto tiempo.

    PARA QUÉ SIRVE: con Google Meet el sistema no sabe nada de quién entró y
    el profesor pasa lista a mano siempre. Aquí, como cada quien entra con su
    cuenta de Dorismon, se registra la presencia y al abrir la asistencia los
    que estuvieron vienen SUGERIDOS como presentes.

    IMPORTANTE: es una sugerencia, no una decisión. El profesor confirma o
    corrige; puede haber quien entró y se fue, o quien tuvo problemas de
    conexión y participó por teléfono.
    """
    __tablename__ = "video_presence"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(ForeignKey("class_sessions.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Se actualiza mientras la persona sigue conectada (cada ~1 minuto)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Minutos acumulados; es lo que decide si se sugiere "presente"
    minutes: Mapped[int] = mapped_column(Integer, default=0)


class PushSubscription(Base):
    """V3.9.29 — La 'dirección de entrega' del teléfono de una persona.

    Cada dispositivo donde acepte recibir avisos genera una. Una misma
    persona puede tener varias (su celular, su tablet, la computadora).
    """
    __tablename__ = "push_subscriptions"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    endpoint: Mapped[str] = mapped_column(Text, unique=True)
    p256dh: Mapped[str] = mapped_column(String)
    auth: Mapped[str] = mapped_column(String)
    device: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AlertAction(Base):
    """V3.9.30 — Lo que el admin ya hizo con una alerta.

    EL PROBLEMA QUE RESUELVE: antes las alertas se quedaban ahí para siempre.
    Una alerta que no se puede resolver deja de ser alerta y se vuelve ruido:
    al tercer día nadie la mira, y tapa las que sí importan.

    Ahora cada alerta tiene salida: resolverla, descartarla o posponerla.
    Queda el registro de qué se hizo y cuándo.
    """
    __tablename__ = "alert_actions"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    # Identifica la alerta: "riesgo:{student_id}", "sin_calificar:{teacher_id}"
    alert_key: Mapped[str] = mapped_column(String, index=True)
    action: Mapped[str] = mapped_column(String)  # resolved | dismissed | snoozed
    note: Mapped[str | None] = mapped_column(String, nullable=True)
    by_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Para "posponer": hasta cuándo queda oculta
    snooze_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MakeupRequest(Base):
    """V3.9.36 — Reponer una clase perdida.

    PARA QUÉ: si el estudiante faltó por algo, o el PROFESOR no llegó, esa
    clase se perdía y punto. Ahora se puede reponer en otra fecha SIN tocar
    la serie: se agrega una clase extra y la recurrencia sigue igual.

    El estudiante la pide, el admin la aprueba y agenda. Así no se llena el
    calendario de reposiciones sin control.
    """
    __tablename__ = "makeup_requests"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    student_id: Mapped[str] = mapped_column(ForeignKey("students.user_id"), index=True)
    # La clase que se perdió. V3.9.37: es OPCIONAL, porque el admin puede
    # agendar una reposición sin una clase concreta detrás (una clase que se
    # le debe al estudiante, algo que nunca se proyectó, etc).
    original_session_id: Mapped[str | None] = mapped_column(
        ForeignKey("class_sessions.id"), nullable=True)
    # pending | approved | rejected | scheduled
    status: Mapped[str] = mapped_column(String, default="pending")
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Quién faltó: "student" o "teacher" (si fue el profe, no cuenta como
    # ausencia del estudiante ni se le cobra)
    missed_by: Mapped[str] = mapped_column(String, default="student")
    preferred_date: Mapped[str | None] = mapped_column(String, nullable=True)
    # La clase de reposición, una vez agendada
    makeup_session_id: Mapped[str | None] = mapped_column(ForeignKey("class_sessions.id"), nullable=True)
    admin_note: Mapped[str | None] = mapped_column(String, nullable=True)
    # V3.9.37 — "student" si la pidió el estudiante, "admin" si la agendó
    # directamente el instituto sin que nadie la solicitara.
    created_by: Mapped[str] = mapped_column(String, default="student", server_default="student")
    # ¿Esta clase avanza el temario del curso, o es solo recuperación?
    # Si repone contenido ya visto, NO debería contar otra vez.
    counts_for_progress: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SessionAudience(Base):
    """V3.9.43 — A quién va dirigida una clase SUELTA.

    EL PROBLEMA QUE RESUELVE: una clase sin serie no pertenece a ningún grupo.
    Con la regla estricta (solo ves lo de tu grupo), esas clases quedaban
    invisibles para TODOS. Se creaba una clase y nadie la veía.

    Ahora al crear una clase suelta se puede decir a quién va: a un grupo, a
    unos estudiantes concretos, o dejarlo abierto a los del profesor en ese
    nivel.
    """
    __tablename__ = "session_audience"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(ForeignKey("class_sessions.id", ondelete="CASCADE"), index=True)
    student_id: Mapped[str] = mapped_column(ForeignKey("students.user_id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class QuizAttemptGrant(Base):
    """V3.9.43 — Intento adicional concedido a UN estudiante.

    Regla de Luis: no se toca el max_attempts global del quiz para darle un
    intento a una sola persona. Se concede una excepción individual, y queda
    registrado quién la autorizó y por qué.
    """
    __tablename__ = "quiz_attempt_grants"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    quiz_id: Mapped[int] = mapped_column(ForeignKey("quizzes.id", ondelete="CASCADE"), index=True)
    student_id: Mapped[str] = mapped_column(ForeignKey("students.user_id", ondelete="CASCADE"), index=True)
    extra_attempts: Mapped[int] = mapped_column(Integer, default=1)
    reason: Mapped[str | None] = mapped_column(String, nullable=True)
    granted_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    used: Mapped[bool] = mapped_column(Boolean, default=False)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


Index("ix_sessions_starts", ClassSession.starts_at_utc)
Index("ix_session_audience", SessionAudience.session_id, SessionAudience.student_id)


class ActivityAudience(Base):
    """V3.9.45 — Audiencia ampliada de una tarea o un quiz.

    PARA QUÉ: `series_id` en la actividad cubre el caso normal (una actividad
    para UN grupo). Esta tabla existe para lo que viene después sin tener que
    rehacer nada:

      - la misma tarea para VARIOS grupos → varias filas con series_id
      - una tarea para estudiantes CONCRETOS → filas con student_id

    Si una actividad tiene filas aquí, mandan estas. Si no tiene, se aplica
    su `series_id`; y si tampoco, va a todos los del profesor en ese nivel
    (que es como se comportan las actividades que ya existen).
    """
    __tablename__ = "activity_audience"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    # "assignment" o "quiz"
    activity_type: Mapped[str] = mapped_column(String, index=True)
    activity_id: Mapped[int] = mapped_column(Integer, index=True)
    # Uno de los dos: el grupo entero, o un estudiante concreto
    series_id: Mapped[str | None] = mapped_column(ForeignKey("class_series.id"), nullable=True)
    student_id: Mapped[str | None] = mapped_column(ForeignKey("students.user_id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


Index("ix_activity_audience", ActivityAudience.activity_type, ActivityAudience.activity_id)
Index("ix_makeup_student", MakeupRequest.student_id, MakeupRequest.status)
Index("ix_alert_actions_key", AlertAction.alert_key)
Index("ix_video_presence_session_user", VideoPresence.session_id, VideoPresence.user_id)
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


class ClassConfirmation(Base):
    """V3.9.21: Confirmación de asistencia del estudiante a una clase próxima.
    El estudiante toca 'Confirmar asistencia' → el profe/admin ve quién confirmó."""
    __tablename__ = "class_confirmations"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(ForeignKey("class_sessions.id", ondelete="CASCADE"))
    student_id: Mapped[str] = mapped_column(ForeignKey("students.user_id", ondelete="CASCADE"))
    confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
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
    """Features editables de un plan (mientras más features, más caro).

    V2.9: Ahora cada feature tiene un `feature_key` que mapea a una funcionalidad real
    del sistema (ej: 'private_classes', 'certificates', 'priority_support').
    El campo `feature` sigue siendo el texto descriptivo legible.
    """
    __tablename__ = "plan_features"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("plans.id", ondelete="CASCADE"))
    feature: Mapped[str] = mapped_column(String)  # Texto descriptivo
    feature_key: Mapped[str | None] = mapped_column(String, nullable=True, index=True)  # V2.9: código interno
    is_included: Mapped[bool] = mapped_column(Boolean, default=True)
    order_index: Mapped[int] = mapped_column(Integer, default=0)


class MessageCategory(str, enum.Enum):
    """V2.0: Categoría del mensaje/ticket."""
    general = "general"           # Mensaje normal
    urgent = "urgent"             # Problema urgente (link no funciona, no entra)
    consultation = "consultation" # Consulta general
    bug = "bug"                   # Bug/error técnico
    request = "request"           # Pedido (cambio horario, etc.)


class MessagePriority(str, enum.Enum):
    """V2.0: Prioridad."""
    low = "low"
    normal = "normal"
    high = "high"


class MessageStatus(str, enum.Enum):
    """V2.0: Estado del ticket (solo aplica si is_ticket=True)."""
    open = "open"           # Recién creado
    in_progress = "in_progress"  # Admin lo está atendiendo
    resolved = "resolved"   # Resuelto
    closed = "closed"       # Cerrado


class Message(Base):
    """V2.0: Mensaje asíncrono entre usuarios o ticket de soporte."""
    __tablename__ = "messages"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    from_user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    to_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=True)
    # NULL = mensaje genérico al admin (ej: ticket de soporte que va a cualquier admin)

    subject: Mapped[str] = mapped_column(String)
    body: Mapped[str] = mapped_column(Text)

    # Para tickets
    is_ticket: Mapped[bool] = mapped_column(Boolean, default=False)
    category: Mapped[MessageCategory] = mapped_column(default=MessageCategory.general)
    priority: Mapped[MessagePriority] = mapped_column(default=MessagePriority.normal)
    status: Mapped[MessageStatus] = mapped_column(default=MessageStatus.open)

    # Thread (replies)
    reply_to_id: Mapped[str | None] = mapped_column(ForeignKey("messages.id", ondelete="SET NULL"), nullable=True)

    # Read tracking
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EmailVerification(Base):
    """V2.1: Códigos de 6 dígitos para verificar email al registrarse.

    Expiran a los 30 minutos. Solo el último válido por usuario.
    """
    __tablename__ = "email_verifications"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    code: Mapped[str] = mapped_column(String, index=True)  # 6 dígitos
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PasswordReset(Base):
    """V2.1: Tokens para resetear contraseña por email.

    Expiran a las 2 horas. Solo el último válido por usuario.
    """
    __tablename__ = "password_resets"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token: Mapped[str] = mapped_column(String, unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
