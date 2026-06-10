"""Seed V1.0 — datos reales de academia: 3 cursos, 6 niveles cada uno,
módulos, lecciones, profesores, estudiantes inscritos, clases programadas,
tareas, quizzes con preguntas, materiales, sedes/aulas, certificado de demo."""
import asyncio
import sys
from datetime import datetime, timedelta, timezone, date

sys.path.insert(0, '.')

from app.core.db import SessionLocal, init_db
from app.core.security import hash_password
from app.models import (
    PlacementQuestion,
    PlanFeature, ModuleProgress,
    User, Student, Teacher, Course, Level, Module, Lesson,
    Branch, Classroom, ClassSession, Enrollment, Quiz, QuizQuestion,
    Assignment, Material, Plan, Payment, Certificate, Notification,
    InstituteSetting,
    UserRole, Modality, SessionStatus, QuestionType, MaterialType,
    PaymentStatus, NotificationType,
)
from secrets import token_urlsafe


ADMIN_EMAIL = "admin@dorismon.do"
ADMIN_PASSWORD = "DorismonAdmin2026!"


def _gen_cert_code():
    return "DRSM-" + token_urlsafe(6).upper().replace("_", "").replace("-", "")[:8]


async def main():
    await init_db()
    from sqlalchemy import select

    async with SessionLocal() as db:
        existing = (await db.execute(select(User).where(User.email == ADMIN_EMAIL))).scalar_one_or_none()
        if existing:
            print("Seed ya corrió. La DB ya tiene datos.")
            return

        # === 1. INSTITUTE SETTINGS ===
        db.add(InstituteSetting(
            id=1, name="Dorismon Language Institute",
            primary_color="#4361ee", accent_color="#f4622a",
            contact_email="contacto@dorismon.do",
            contact_phone="+1 809 555 0100",
            address="Santo Domingo, RD",
        ))

        # === 2. ADMIN ===
        admin = User(
            email=ADMIN_EMAIL, password_hash=hash_password(ADMIN_PASSWORD),
            full_name="Administrador Dorismon", role=UserRole.super_admin,
        )
        db.add(admin)
        await db.flush()
        print(f"Admin: {ADMIN_EMAIL} / {ADMIN_PASSWORD}")

        # === 3. PROFESORES ===
        teachers_data = [
            ("ana@dorismon.do", "Ana Martínez", "speaking,pronunciation", "online,hibrida",
             "Especialista en fluidez y pronunciación, 8 años de experiencia."),
            ("luis@dorismon.do", "Luis Reyes", "grammar,writing", "presencial",
             "Profesor de gramática y escritura, CELTA certified."),
            ("sara@dorismon.do", "Sara Núñez", "listening,toefl,ielts", "online",
             "Especialista en preparación de exámenes internacionales."),
        ]
        teacher_ids = {}
        for em, name, spec, mods, bio in teachers_data:
            u = User(email=em, password_hash=hash_password("Profe2026!"),
                    full_name=name, role=UserRole.teacher)
            db.add(u)
            await db.flush()
            db.add(Teacher(user_id=u.id, specialties=spec, modalities=mods, bio=bio))
            await db.flush()  # Teacher debe existir antes de ser referenciado como FK
            teacher_ids[em] = u.id
            print(f"Profesor: {em} / Profe2026!")

        # === 4. CURSOS ===
        courses_data = [
            ("english-general", "Inglés General", "Programa completo de inglés general A1-C2", "#4361ee"),
            ("business-english", "Inglés para Negocios", "Comunicación profesional y empresarial", "#f4622a"),
            ("toefl-prep", "Preparación TOEFL", "Preparación intensiva para el examen TOEFL iBT", "#1d9e75"),
        ]
        course_ids = {}
        for code, name, desc, color in courses_data:
            c = Course(code=code, name=name, description=desc, color=color, order_index=len(course_ids))
            db.add(c)
            await db.flush()
            course_ids[code] = c.id
        print(f"{len(courses_data)} cursos")

        # === 5. NIVELES (A1-C2 para cada curso) ===
        levels_meta = [
            ("A1", "Principiante", "Primeras palabras y frases", 120),
            ("A2", "Básico", "Conversaciones cotidianas", 120),
            ("B1", "Intermedio", "Comunicación con soltura", 150),
            ("B2", "Intermedio alto", "Fluidez en contextos variados", 150),
            ("C1", "Avanzado", "Nivel profesional", 180),
            ("C2", "Maestría", "Casi nativo", 180),
        ]
        level_ids = {}  # (course_code, level_code) -> level_id
        for course_code, course_id in course_ids.items():
            for i, (lvl_code, lvl_name, lvl_desc, hours) in enumerate(levels_meta):
                l = Level(course_id=course_id, code=lvl_code, name=lvl_name,
                          description=lvl_desc, hours_required=hours, order_index=i)
                db.add(l)
                await db.flush()
                level_ids[(course_code, lvl_code)] = l.id
        print(f"{len(level_ids)} niveles totales")

        # === 6. MÓDULOS Y LECCIONES (solo para Inglés General A1, A2, B1) ===
        # Reducido para no inflar el seed
        modules_per_level = ["Gramática", "Conversación", "Comprensión Auditiva"]
        focus_options = ["grammar", "speaking", "listening"]
        lessons_data = {
            "A1": [
                ("Saludos y presentaciones", "Puedo presentarme y saludar", 12),
                ("El alfabeto y números", "Reconozco letras y números", 10),
                ("Verbo 'to be'", "Uso 'am/is/are' correctamente", 15),
                ("Mi familia", "Puedo describir a mi familia", 14),
                ("Días y fechas", "Puedo decir fechas en inglés", 12),
            ],
            "A2": [
                ("Pasado simple regular", "Hablo de hechos pasados", 18),
                ("De compras", "Pido cosas en una tienda", 15),
                ("Direcciones", "Entiendo cómo llegar a un lugar", 16),
                ("Rutina diaria", "Describo mi día a día", 14),
                ("Comparativos", "Comparo dos cosas", 17),
            ],
            "B1": [
                ("Polite requests", "Pido cosas con cortesía", 18),
                ("Present perfect", "Distingo cuándo usarlo", 22),
                ("News listening", "Capto noticias breves", 20),
                ("Opinions", "Doy y defiendo mi opinión", 20),
                ("Conditionals", "Uso if-clauses", 24),
            ],
        }
        lesson_id_first_a1 = None
        for course_code in ("english-general",):
            for lvl_code in ("A1", "A2", "B1"):
                level_id = level_ids[(course_code, lvl_code)]
                module_ids = []
                for j, mname in enumerate(modules_per_level):
                    m = Module(level_id=level_id, name=mname, order_index=j,
                              description=f"Módulo de {focus_options[j]}")
                    db.add(m)
                    await db.flush()
                    module_ids.append(m.id)
                for k, (title, can_do, dur) in enumerate(lessons_data[lvl_code]):
                    lesson = Lesson(
                        module_id=module_ids[k % 3], title=title,
                        description=f"Lección {k+1} del nivel {lvl_code}",
                        objectives=f"Aprender: {can_do}", can_do=can_do,
                        duration_min=dur, order_index=k,
                        video_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                    )
                    db.add(lesson)
                    await db.flush()
                    if lvl_code == "A1" and k == 0:
                        lesson_id_first_a1 = lesson.id
        print("Lecciones creadas (A1, A2, B1 de Inglés General)")

        # === 7. PLANES ===
        plans = [
            Plan(code="starter", name="Starter", price=29.00, duration_months=1,
                 description="4 clases al mes", features="• 4 clases\n• Acceso a lecciones\n• Material básico"),
            Plan(code="professional", name="Professional", price=69.00, duration_months=1,
                 description="8 clases al mes", features="• 8 clases\n• Todas las lecciones\n• Quizzes\n• Tareas con feedback"),
            Plan(code="academy", name="Academy", price=129.00, duration_months=1,
                 description="Clases ilimitadas", features="• Clases ilimitadas\n• Todos los cursos\n• Certificados oficiales\n• Asesoría 1:1"),
        ]
        db.add_all(plans)
        await db.flush()
        print(f"{len(plans)} planes")

        # === 7b. Features estructuradas por plan (V1.3) ===
        plan_features_data = {
            "starter": [
                ("8 clases grupales al mes (2 por semana)", True, 0),
                ("Modalidad: solo online", True, 1),
                ("Acceso a material básico", True, 2),
                ("Eventos abiertos", False, 3),
                ("Material descargable", False, 4),
                ("Certificado al final del nivel", False, 5),
            ],
            "professional": [
                ("16 clases grupales al mes (4 por semana)", True, 0),
                ("Modalidad: online + híbrida", True, 1),
                ("Hasta 2 eventos abiertos al mes", True, 2),
                ("Material descargable completo", True, 3),
                ("Certificado al final del nivel", True, 4),
                ("Soporte por email del profesor", True, 5),
                ("Acceso a TOEFL / Business", False, 6),
            ],
            "academy": [
                ("24 clases grupales al mes (6 por semana)", True, 0),
                ("Modalidad: online + presencial + híbrida", True, 1),
                ("Eventos abiertos ilimitados", True, 2),
                ("Material descargable + recursos premium", True, 3),
                ("Certificado al final del nivel", True, 4),
                ("Soporte directo del profesor", True, 5),
                ("1 clase privada al mes", True, 6),
                ("Acceso a TOEFL / Business / Conversación", True, 7),
            ],
        }
        for plan in plans:
            for feature_text, is_inc, idx in plan_features_data.get(plan.code, []):
                db.add(PlanFeature(plan_id=plan.id, feature=feature_text, is_included=is_inc, order_index=idx))
        print("Features de planes cargadas")

        # === 8. SEDES Y AULAS ===
        branches_data = [
            ("Sede Piantini", "Av. Tiradentes 123, Piantini, Santo Domingo", "+1 809 555 0101"),
            ("Sede Zona Colonial", "Calle El Conde 45, Zona Colonial", "+1 809 555 0102"),
        ]
        branch_ids = []
        for bname, addr, phone in branches_data:
            b = Branch(name=bname, address=addr, phone=phone)
            db.add(b)
            await db.flush()
            branch_ids.append(b.id)
            for i in range(1, 4):
                db.add(Classroom(branch_id=b.id, name=f"Aula {i}", capacity=12))
        print(f"{len(branches_data)} sedes con aulas")

        # === 9. ESTUDIANTES DE PRUEBA ===
        students_data = [
            ("maria.estudiante@dorismon.do", "María Rodríguez", "B1", "english-general"),
            ("carlos.estudiante@dorismon.do", "Carlos Pérez", "A2", "english-general"),
            ("juana.estudiante@dorismon.do", "Juana Méndez", "A1", "english-general"),
        ]
        student_ids = {}
        for em, name, lvl_code, course_code in students_data:
            u = User(email=em, password_hash=hash_password("Estudiante2026!"),
                    full_name=name, role=UserRole.student)
            db.add(u)
            await db.flush()
            level_id = level_ids[(course_code, lvl_code)]
            s = Student(user_id=u.id, current_level_id=level_id, placement_done=True,
                       speaking_score=70.0, listening_score=72.0, reading_score=75.0, writing_score=68.0)
            db.add(s)
            await db.flush()  # asegurar que Student exista antes del FK del Enrollment
            student_ids[em] = u.id
            # Inscripción
            db.add(Enrollment(
                student_id=u.id, course_id=course_ids[course_code],
                level_id=level_id, teacher_id=teacher_ids["ana@dorismon.do"],
            ))
            print(f"Estudiante: {em} / Estudiante2026!")

        # === 10. CLASES PROGRAMADAS (próximos 14 días) ===
        now = datetime.now(timezone.utc)
        sessions_data = [
            (1, 9, "english-general", "B1", "ana@dorismon.do", Modality.online,
             "B1 - Polite requests", "https://meet.google.com/abc-defg-hij", None, None),
            (1, 14, "english-general", "A2", "luis@dorismon.do", Modality.presencial,
             "A2 - Past simple", None, branch_ids[0], None),
            (2, 10, "english-general", "A1", "luis@dorismon.do", Modality.online,
             "A1 - Saludos", "https://zoom.us/j/123456", None, None),
            (3, 9, "english-general", "B1", "ana@dorismon.do", Modality.online,
             "B1 - Present perfect", "https://meet.google.com/abc-defg-hij", None, None),
            (4, 18, "english-general", "A2", "luis@dorismon.do", Modality.hibrida,
             "A2 - Conversación", "https://zoom.us/j/789", branch_ids[1], None),
            (5, 9, "english-general", "B1", "ana@dorismon.do", Modality.online,
             "B1 - News listening", "https://meet.google.com/abc-defg-hij", None, None),
            (7, 10, "english-general", "A1", "luis@dorismon.do", Modality.presencial,
             "A1 - Mi familia", None, branch_ids[0], None),
            (8, 9, "english-general", "B1", "ana@dorismon.do", Modality.online,
             "B1 - Opinions", "https://meet.google.com/abc-defg-hij", None, None),
            (10, 14, "english-general", "A2", "luis@dorismon.do", Modality.presencial,
             "A2 - Rutina diaria", None, branch_ids[0], None),
            (12, 9, "english-general", "B1", "ana@dorismon.do", Modality.online,
             "B1 - Conditionals", "https://meet.google.com/abc-defg-hij", None, None),
        ]
        for day, hour, course_code, lvl_code, t_email, mod, title, mtg_url, b_id, c_id in sessions_data:
            start = (now + timedelta(days=day)).replace(hour=hour, minute=0, second=0, microsecond=0)
            end = start + timedelta(minutes=90)
            db.add(ClassSession(
                course_id=course_ids[course_code], level_id=level_ids[(course_code, lvl_code)],
                teacher_id=teacher_ids[t_email], title=title, modality=mod,
                starts_at_utc=start, ends_at_utc=end,
                meeting_url=mtg_url, branch_id=b_id, classroom_id=c_id, capacity=12,
            ))
        print(f"{len(sessions_data)} clases programadas")

        # === 11. TAREAS Y QUIZZES ===
        # Tarea 1
        db.add(Assignment(
            title="Ensayo: Mi rutina del fin de semana",
            description="Escribe 150-200 palabras describiendo qué haces los fines de semana.",
            instructions="• Mínimo 150 palabras\n• Usa al menos 5 verbos en presente simple\n• Incluye actividades de tu familia",
            teacher_id=teacher_ids["luis@dorismon.do"],
            level_id=level_ids[("english-general", "A2")],
            max_score=100, due_at=now + timedelta(days=5),
        ))
        db.add(Assignment(
            title="Reading: Opinion piece",
            description="Lee el artículo adjunto y escribe tu opinión.",
            instructions="• Mínimo 200 palabras\n• Argumenta a favor o en contra\n• Usa conectores",
            teacher_id=teacher_ids["ana@dorismon.do"],
            level_id=level_ids[("english-general", "B1")],
            max_score=100, due_at=now + timedelta(days=7),
        ))

        # Quiz B1 con preguntas de varios tipos
        quiz = Quiz(
            title="Quiz: Present Perfect vs Past Simple",
            description="Evaluación rápida sobre los tiempos verbales más complicados de B1.",
            teacher_id=teacher_ids["ana@dorismon.do"],
            level_id=level_ids[("english-general", "B1")],
            passing_score=70.0, max_attempts=3, is_published=True,
        )
        db.add(quiz)
        await db.flush()
        questions = [
            (QuestionType.multiple_choice, "I _____ to Paris twice.",
             ["have been", "went", "was", "have went"], "have been", 10),
            (QuestionType.multiple_choice, "She _____ her keys yesterday.",
             ["has lost", "lost", "loses", "is losing"], "lost", 10),
            (QuestionType.true_false, "'Have you ever eaten sushi?' is present perfect.",
             ["True", "False"], "True", 10),
            (QuestionType.fill_blank, "Complete: 'I _____ (live) here for 5 years.'",
             None, "have lived", 10),
            (QuestionType.short_answer, "Translate: 'Yo nunca he visitado España.'",
             None, "I have never visited Spain", 10),
        ]
        for i, (qtype, statement, opts, correct, pts) in enumerate(questions):
            db.add(QuizQuestion(
                quiz_id=quiz.id, type=qtype, statement=statement,
                options=opts, correct_answer=correct, points=pts, order_index=i,
            ))
        print("Quiz B1 con 5 preguntas + 2 tareas")

        # === 12. MATERIALES (biblioteca) ===
        materials_data = [
            ("Guía completa de Present Perfect", "PDF descargable con explicaciones y ejercicios",
             MaterialType.pdf, "https://example.com/present-perfect.pdf",
             course_ids["english-general"], level_ids[("english-general", "B1")]),
            ("Video: Pronunciación del /th/", "Tutorial práctico de 8 minutos",
             MaterialType.video, "https://www.youtube.com/watch?v=xY3PzZqgX5w",
             course_ids["english-general"], level_ids[("english-general", "B1")]),
            ("Audio: Vocabulario A1", "Lista de palabras esenciales con pronunciación",
             MaterialType.audio, "https://example.com/audio-a1.mp3",
             course_ids["english-general"], level_ids[("english-general", "A1")]),
            ("Plantilla: Carta formal en inglés", "Modelo para cartas profesionales",
             MaterialType.document, "https://example.com/carta-formal.docx",
             course_ids["business-english"], None),
        ]
        for title, desc, mtype, url, cid, lid in materials_data:
            db.add(Material(
                title=title, description=desc, type=mtype, url=url,
                course_id=cid, level_id=lid, uploaded_by=teacher_ids["ana@dorismon.do"],
                is_public=True,
            ))
        print(f"{len(materials_data)} materiales en biblioteca")

        # === 13. UN PAGO PAGADO DEMO ===
        first_student = student_ids["maria.estudiante@dorismon.do"]
        db.add(Payment(
            student_id=first_student, plan_id=plans[1].id,
            amount=69.00, status=PaymentStatus.paid, method="transfer",
            paid_at=now - timedelta(days=10),
        ))
        db.add(Payment(
            student_id=student_ids["carlos.estudiante@dorismon.do"], plan_id=plans[0].id,
            amount=29.00, status=PaymentStatus.pending, method="stripe",
        ))

        # === 14. UNA NOTIFICACIÓN DE BIENVENIDA PARA CADA ESTUDIANTE ===
        for em, sid in student_ids.items():
            db.add(Notification(
                user_id=sid, type=NotificationType.info,
                title="¡Bienvenido a Dorismon!",
                body="Explorá tus clases, tareas y materiales. ¡Mucho éxito!",
                link="/dashboard/student",
            ))



        # === 14b. EVENTOS ABIERTOS (cualquier estudiante puede registrarse) ===
        events_data = [
            (1, 19, "english-general", "B1", "ana@dorismon.do", Modality.online,
             "Conversation Club: Travel Stories",
             "Practicá tu inglés conversando sobre experiencias de viaje. Abierto a niveles B1+.",
             "https://meet.google.com/event-conv-1", None, None, 20),
            (3, 18, "english-general", "A2", "luis@dorismon.do", Modality.presencial,
             "Taller: Pronunciación del 'th'",
             "Aprende a pronunciar correctamente el sonido más difícil del inglés. Abierto a todos los niveles.",
             None, branch_ids[0], None, 12),
            (5, 17, "business-english", "B2", "sara@dorismon.do", Modality.online,
             "Office Hours con Sara",
             "Sesión de preguntas y respuestas con Sara sobre TOEFL. Traé tus dudas.",
             "https://zoom.us/j/office-hours", None, None, 25),
            (7, 16, "english-general", "B1", "ana@dorismon.do", Modality.hibrida,
             "Movie Night: An Inglés Subtitulado",
             "Vemos una película en inglés y la discutimos. Lugar híbrido.",
             "https://meet.google.com/event-movie", branch_ids[1], None, 30),
        ]
        for day, hour, course_code, lvl_code, t_email, mod, title, desc, mtg_url, b_id, c_id, cap in events_data:
            start = (now + timedelta(days=day)).replace(hour=hour, minute=0, second=0, microsecond=0)
            end = start + timedelta(minutes=60)
            db.add(ClassSession(
                course_id=course_ids[course_code], level_id=level_ids[(course_code, lvl_code)],
                teacher_id=teacher_ids[t_email], title=title, description=desc, modality=mod,
                starts_at_utc=start, ends_at_utc=end,
                meeting_url=mtg_url, branch_id=b_id, classroom_id=c_id, capacity=cap,
                is_open_event=True,  # EVENTO ABIERTO
            ))
        print(f"{len(events_data)} eventos abiertos creados")

        # === 15. PLACEMENT QUESTIONS (60 preguntas balanceadas A1-C1) ===
        # Distribución: A1=12, A2=12, B1=14, B2=14, C1=8
        # Por skill: grammar=30, reading=20, use_of_english=10
        placement_questions = [
            # ============ A1 (12 preguntas) ============
            # Grammar (6)
            ("My name ___ Maria.", "is", "are", "am", "be", "a", "A1", "grammar"),
            ("How ___ you? — I'm fine, thanks.", "is", "are", "be", "do", "b", "A1", "grammar"),
            ("She ___ a teacher.", "are", "is", "am", "be", "b", "A1", "grammar"),
            ("I ___ from Mexico.", "am", "is", "are", "be", "a", "A1", "grammar"),
            ("They ___ students.", "is", "am", "are", "be", "c", "A1", "grammar"),
            ("___ you like coffee?", "Are", "Is", "Do", "Does", "c", "A1", "grammar"),
            # Vocabulary / Use of English (3)
            ("This is ___ apple.", "a", "an", "the", "any", "b", "A1", "use_of_english"),
            ("I have ___ brother.", "a", "an", "one", "any", "a", "A1", "use_of_english"),
            ("There ___ a cat in the garden.", "is", "are", "am", "be", "a", "A1", "use_of_english"),
            # Reading (3) — frases cortas, comprensión inmediata
            ("Read: 'Tom is 8 years old. He goes to school.' Tom is a...", "teacher", "child", "doctor", "father", "b", "A1", "reading"),
            ("Read: 'Anna lives in Paris. She speaks French.' What language does Anna speak?", "English", "Spanish", "French", "German", "c", "A1", "reading"),
            ("Read: 'I eat breakfast at 8 AM.' When do I eat breakfast?", "at night", "in the morning", "in the afternoon", "at lunch", "b", "A1", "reading"),

            # ============ A2 (12 preguntas) ============
            # Grammar (6)
            ("I ___ to the store yesterday.", "go", "goes", "went", "gone", "c", "A2", "grammar"),
            ("She is ___ than her sister.", "tall", "taller", "tallest", "more tall", "b", "A2", "grammar"),
            ("If it ___ tomorrow, we won't go to the beach.", "rain", "rains", "rained", "will rain", "b", "A2", "grammar"),
            ("They ___ TV when I arrived.", "watched", "were watching", "watch", "are watching", "b", "A2", "grammar"),
            ("I ___ to swim when I was 6.", "can", "could", "must", "should", "b", "A2", "grammar"),
            ("This is the ___ movie I've ever seen.", "good", "better", "best", "more good", "c", "A2", "grammar"),
            # Use of English (3)
            ("She's afraid ___ spiders.", "of", "to", "with", "from", "a", "A2", "use_of_english"),
            ("I'm looking ___ my keys.", "at", "to", "for", "on", "c", "A2", "use_of_english"),
            ("Could you ___ me a favor?", "make", "do", "take", "have", "b", "A2", "use_of_english"),
            # Reading (3)
            ("Read: 'The restaurant opens at 7 PM and closes at midnight.' How many hours is it open?", "3", "5", "7", "10", "b", "A2", "reading"),
            ("Read: 'Sara loves reading. She has more than 200 books at home.' Sara probably enjoys...", "sports", "literature", "cooking", "music", "b", "A2", "reading"),
            ("Read: 'Don't enter. Wet floor.' This sign means you should...", "be careful", "leave quickly", "clean the floor", "wait outside", "a", "A2", "reading"),

            # ============ B1 (14 preguntas) ============
            # Grammar (7)
            ("I ___ here for five years.", "live", "am living", "have lived", "lived", "c", "B1", "grammar"),
            ("She told me ___ wait for her.", "to", "for", "that", "about", "a", "B1", "grammar"),
            ("If I had time, I ___ help you.", "will", "would", "had", "have", "b", "B1", "grammar"),
            ("The car ___ in Germany.", "made", "is made", "is making", "makes", "b", "B1", "grammar"),
            ("I'm used ___ early.", "to wake up", "to waking up", "waking up", "wake up", "b", "B1", "grammar"),
            ("You ___ work today, it's a holiday.", "don't have to", "mustn't", "shouldn't", "won't", "a", "B1", "grammar"),
            ("Neither John ___ his brother came to the party.", "or", "nor", "and", "but", "b", "B1", "grammar"),
            # Use of English (3)
            ("The book ___ I bought yesterday is great.", "what", "which", "who", "whose", "b", "B1", "use_of_english"),
            ("She apologized ___ being late.", "of", "about", "for", "with", "c", "B1", "use_of_english"),
            ("I'd rather ___ home tonight.", "stay", "to stay", "staying", "stayed", "a", "B1", "use_of_english"),
            # Reading (4)
            ("Read: 'Despite the heavy rain, the concert continued.' What happened?", "concert was cancelled", "concert continued", "rain stopped", "people left", "b", "B1", "reading"),
            ("Read: 'Recycling helps reduce waste and protect natural resources.' Recycling is...", "expensive", "beneficial", "complicated", "rare", "b", "B1", "reading"),
            ("Read: 'The restaurant offers a 20% discount for students with valid ID.' Who gets the discount?", "everyone", "tourists", "students with ID", "regular customers", "c", "B1", "reading"),
            ("Read: 'Online learning is becoming increasingly popular among working adults.' What does this say?", "Adults dislike learning", "Adults learn more online", "Online learning is decreasing", "Adults can't study", "b", "B1", "reading"),

            # ============ B2 (14 preguntas) ============
            # Grammar (7)
            ("If I ___ more money, I would travel the world.", "have", "had", "would have", "had had", "b", "B2", "grammar"),
            ("By the time you arrive, I ___ finished the report.", "will have", "have", "had", "will", "a", "B2", "grammar"),
            ("She wishes she ___ taller.", "is", "were", "would be", "had been", "b", "B2", "grammar"),
            ("___ I had studied more, I would have passed.", "If", "Had", "When", "While", "b", "B2", "grammar"),
            ("The report needs ___ before Friday.", "finish", "to finish", "finishing", "finished", "c", "B2", "grammar"),
            ("I don't mind ___ overtime if needed.", "to work", "working", "work", "worked", "b", "B2", "grammar"),
            ("He's not ___ to drive yet.", "old enough", "enough old", "as old", "so old", "a", "B2", "grammar"),
            # Use of English (3)
            ("She managed ___ the meeting despite traffic.", "attending", "to attend", "attend", "attended", "b", "B2", "use_of_english"),
            ("___ of the students passed the exam.", "Each", "Every", "All", "Whole", "c", "B2", "use_of_english"),
            ("The decision was made ___ careful consideration.", "without", "after", "during", "since", "b", "B2", "use_of_english"),
            # Reading (4)
            ("Read: 'The new policy aims to streamline operations and reduce overhead costs by 15%.' The policy intends to...", "increase costs", "make work more efficient", "hire more staff", "change products", "b", "B2", "reading"),
            ("Read: 'Although the proposal seemed promising, it lacked sufficient supporting data.' The proposal was...", "fully approved", "rejected outright", "incomplete", "implemented", "c", "B2", "reading"),
            ("Read: 'The candidate's qualifications, albeit impressive, did not align with the position's requirements.' The candidate was...", "perfect for the job", "not a match", "rejected for low skills", "hired immediately", "b", "B2", "reading"),
            ("Read: 'Researchers contend that excessive screen time may impair cognitive development in children.' Researchers...", "support screens", "are uncertain", "argue screens harm kids", "found no link", "c", "B2", "reading"),

            # ============ C1 (8 preguntas) ============
            # Grammar (4)
            ("Had I known about the meeting, I ___ attended.", "would have", "had", "have", "will have", "a", "C1", "grammar"),
            ("Not until later ___ the true cost of the project.", "we realized", "did we realize", "we did realize", "realized we", "b", "C1", "grammar"),
            ("Rarely ___ such a remarkable performance.", "I have seen", "have I seen", "I had seen", "had I seen", "b", "C1", "grammar"),
            ("Were it not ___ your help, I would have failed.", "for", "of", "to", "with", "a", "C1", "grammar"),
            # Use of English (1)
            ("She is hardly ___ to manage such complex situations.", "able", "capable", "skilled", "competent", "b", "C1", "use_of_english"),
            # Reading (3)
            ("Read: 'The author's nuanced critique transcends mere partisan rhetoric, offering substantive policy alternatives.' The critique is...", "biased", "shallow", "thoughtful and constructive", "purely political", "c", "C1", "reading"),
            ("Read: 'Notwithstanding the initial setbacks, the company has demonstrated remarkable resilience.' The company...", "failed completely", "recovered well despite problems", "had no problems", "is still struggling", "b", "C1", "reading"),
            ("Read: 'The proposal was met with scant enthusiasm from the committee, prompting a thorough revision.' The reaction was...", "very positive", "neutral", "lukewarm at best", "hostile", "c", "C1", "reading"),
        ]
        for i, (stmt, oa, ob, oc, od, correct, lvl, skill) in enumerate(placement_questions):
            db.add(PlacementQuestion(
                statement=stmt, option_a=oa, option_b=ob, option_c=oc, option_d=od,
                correct_option=correct, difficulty_level=lvl, skill=skill,
                order_index=i,
            ))
        print(f"{len(placement_questions)} preguntas de placement test")

        await db.commit()

        print("\n=== SEED COMPLETO ===")
        print(f"ADMIN: {ADMIN_EMAIL} / {ADMIN_PASSWORD}")
        print(f"PROFES (Profe2026!): ana@dorismon.do, luis@dorismon.do, sara@dorismon.do")
        print(f"ESTUDIANTES (Estudiante2026!): maria.estudiante@dorismon.do, carlos.estudiante@dorismon.do, juana.estudiante@dorismon.do")


if __name__ == "__main__":
    asyncio.run(main())
