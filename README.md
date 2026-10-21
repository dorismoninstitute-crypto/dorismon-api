# Dorismon Language Institute — V1.0

Plataforma de gestión académica para academia de inglés. Modalidades **online, presencial e híbrida**.

## Arquitectura

- **Backend**: FastAPI + SQLAlchemy 2.0 async + PostgreSQL + JWT
- **Frontend**: Next.js 14 + TypeScript + Tailwind CSS
- **Despliegue**: Render (backend) + Vercel (frontend)

## Roles del sistema (solo 3)

| Rol | Acceso |
|---|---|
| `super_admin` | Control total: usuarios, cursos, sedes, finanzas, configuración |
| `teacher` | Sus clases, calificar, materiales, observaciones |
| `student` | Sus cursos, tareas, quizzes, calendario, certificados |

## Modelo académico

```
Course (Inglés General, Business, TOEFL, etc — configurables)
   ↓
Level (A1-C2 — asociados al curso)
   ↓
Module (Grammar, Speaking, etc — configurables)
   ↓
Lesson (con video, PDF, audio, recursos)
```

## Datos de desarrollo (seed)

`python seed.py` crea cuentas **solo de desarrollo y pruebas** (admin, 3 profesores y 3 estudiantes
con correos `@dorismon.do`). El seed **no trae contraseñas**: exige `ALLOW_DEV_SEED=true` y que definas tú
`SEED_ADMIN_PASSWORD`, `SEED_TEACHER_PASSWORD` y `SEED_STUDENT_PASSWORD` (si falta alguna, se niega a correr). No imprime contraseñas.

```
ADMIN:        admin@dorismon.do        / <SEED_ADMIN_PASSWORD>
PROFES:       ana@ / luis@ / sara@dorismon.do                 / <SEED_TEACHER_PASSWORD>
ESTUDIANTES:  maria.estudiante@ / carlos.estudiante@ / juana.estudiante@dorismon.do / <SEED_STUDENT_PASSWORD>
```

**Producción:** el seed se niega a ejecutarse (Render / `ENVIRONMENT=production`). Las cuentas reales se
crean con contraseñas propias. Ver `PENDIENTES_DEPLOY.md` antes de desplegar.

## Estructura

```
backend/
├── app/
│   ├── core/        # db, config, security
│   ├── models/      # 25 tablas SQLAlchemy
│   ├── routers/     # auth, admin, teacher, student, catalog, certificates
│   └── services/    # audit
├── seed.py          # datos iniciales
└── requirements.txt

frontend/
├── app/
│   ├── (públicas)   # /, /login, /register, /certificate/[code]
│   └── dashboard/   # admin, teacher, student
├── components/      # ui.tsx, ErrorBoundary
├── lib/api.ts       # cliente API tipado
└── tailwind.config.js
```

## Stack y razones

- **FastAPI** porque async, rápido, docs automáticos
- **SQLAlchemy async** porque maneja bien la concurrencia
- **PostgreSQL** porque es la opción más estable para relaciones complejas
- **Next.js 14 + App Router** porque RSC + Tailwind funcionan bien para LMS
- **Tailwind** porque permite consistencia visual sin CSS personalizado

## Características V1.0 incluidas

- ✅ 3 roles con permisos finos
- ✅ Curso → Nivel → Módulo → Lección configurables
- ✅ Quizzes con 4 tipos de pregunta (multi-opción, V/F, completar, respuesta corta) y corrección automática
- ✅ Tareas con submission y calificación
- ✅ Biblioteca de materiales (PDF, video, audio, link)
- ✅ Clases online/presencial/híbrida con Zoom/Meet/Teams
- ✅ Sedes y aulas
- ✅ Asistencia con 4 estados
- ✅ Expediente académico completo del estudiante
- ✅ Certificados con código único y verificación pública
- ✅ Notificaciones internas
- ✅ Calendario académico
- ✅ Panel financiero
- ✅ Auditoría
- ✅ Tailwind responsive (desktop, tablet, mobile)
- ✅ Loading, Empty y Error states en todas las pantallas

## NO incluido en V1.0 (planificado para futuras versiones)

- Stripe activo (modelo preparado, sin webhook)
- Upload real de archivos a S3 (los campos URL están, pero el upload directo no)
- Notificaciones por email/WhatsApp/SMS
- Generación PDF del certificado (datos listos, sin PDF físico)
- Reportes exportables Excel/PDF
- Multi-instituto / white label
- Director y Coordinador como roles separados
- CRM completo
- IA / automatizaciones
- Videollamada propia (usa Zoom/Meet/Teams externos)

## Despliegue

Ver `DEPLOYMENT.md` para guía completa de Render + Vercel.

## Documentación adicional

- `API_DOCS.md` — endpoints disponibles
- `DATABASE_SCHEMA.md` — esquema de tablas
- `ADMIN_GUIDE.md` — guía operativa para el admin
- `TEACHER_GUIDE.md` — guía operativa para profesores
