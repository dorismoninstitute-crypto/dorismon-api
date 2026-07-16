"""Test de sincronización entre roles (V3.9.20).
Garantiza que no regresen los bugs encontrados en la auditoría multi-rol:
1. El dashboard del profe saluda AL PROFE (no a un estudiante) — bug V3.9.18
2. Clase finalizada por el profe deja de ser "próxima" para el estudiante
3. Cancelación por admin genera aviso + notificación al estudiante
4. Calificar exige nota válida
Uso: python tests/test_role_sync.py http://localhost:8000
"""
import asyncio, sys, datetime
import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
PASS = 0
FAIL = 0

def check(name, ok):
    global PASS, FAIL
    print(f"  {'✓' if ok else '✗'} {name}")
    if ok: PASS += 1
    else: FAIL += 1

async def main():
    async with httpx.AsyncClient(base_url=BASE, timeout=30) as c:
        admin = (await c.post("/auth/login", json={"email":"admin@dorismon.do","password":"DorismonAdmin2026!"})).json()["access_token"]
        AH = {"Authorization": f"Bearer {admin}"}
        ml = (await c.post("/auth/login", json={"email":"maria.estudiante@dorismon.do","password":"Estudiante2026!"})).json()
        MH = {"Authorization": f"Bearer {ml['access_token']}"}
        mu = (await c.get("/admin/users?q=maria.estudiante", headers=AH)).json()
        maria_id = mu["items"][0]["id"]
        courses = (await c.get("/admin/courses", headers=AH)).json()
        cid = courses[0]["id"]
        levels = (await c.get(f"/admin/levels-by-course/{cid}", headers=AH)).json()
        lvls = levels["items"] if isinstance(levels, dict) else levels
        prof_m = (await c.get(f"/admin/students/{maria_id}/profile", headers=AH)).json()
        m_lvl = [l for l in lvls if l["code"] == prof_m.get("current_level_code")]
        m_lvl = m_lvl[0] if m_lvl else lvls[0]
        profes = (await c.get("/admin/users?role=teacher", headers=AH)).json()
        seed_profes = [p for p in profes["items"] if p["email"] in ("ana@dorismon.do", "luis@dorismon.do", "sara@dorismon.do")]
        profe = seed_profes[0] if seed_profes else profes["items"][0]
        tid, temail = profe["id"], profe["email"]
        tl = (await c.post("/auth/login", json={"email": temail, "password":"Profe2026!"})).json()
        TH = {"Authorization": f"Bearer {tl['access_token']}"}
        now_utc = datetime.datetime.now(datetime.timezone.utc)

        # 1. El dashboard del profe saluda AL PROFE (aunque tenga estudiantes asignados)
        await c.post("/admin/enrollments", headers=AH, json={
            "student_id": maria_id, "course_id": cid, "level_id": m_lvl["id"], "teacher_id": tid,
        })
        tdash = (await c.get("/teacher/dashboard", headers=TH)).json()
        hero_name = tdash.get("user", {}).get("full_name", "")
        check("dashboard del profe saluda al profe (no a un estudiante)",
              hero_name == profe["full_name"])

        # 2. Finalizada por el profe → deja de ser próxima del estudiante
        st = (now_utc - datetime.timedelta(minutes=15)).isoformat()
        en = (now_utc + datetime.timedelta(minutes=60)).isoformat()
        cr = await c.post("/admin/sessions", headers=AH, json={
            "title": "Sync Finalize", "starts_at_utc": st, "ends_at_utc": en,
            "modality": "online", "meeting_url": "https://meet.google.com/s",
            "teacher_id": tid, "course_id": cid, "level_id": m_lvl["id"],
        })
        sid = cr.json()["id"]
        await c.post(f"/teacher/sessions/{sid}/finalize", headers=TH)
        prog = (await c.get("/progress/my-course", headers=MH)).json()
        ns = prog.get("next_session") or {}
        check("clase finalizada ya no es la próxima del estudiante", ns.get("id") != sid)

        # 3. Cancelación por admin → aviso + notificación
        st2 = (now_utc + datetime.timedelta(days=1)).isoformat()
        en2 = (now_utc + datetime.timedelta(days=1, hours=1)).isoformat()
        cr2 = await c.post("/admin/sessions", headers=AH, json={
            "title": "Sync Cancel", "starts_at_utc": st2, "ends_at_utc": en2,
            "modality": "online", "meeting_url": "https://meet.google.com/c",
            "teacher_id": tid, "course_id": cid, "level_id": m_lvl["id"],
        })
        sid2 = cr2.json()["id"]
        await c.delete(f"/admin/sessions/{sid2}", headers=AH)
        sdash = (await c.get("/student/dashboard", headers=MH)).json()
        rc = [x for x in sdash.get("recent_cancelled", []) if x.get("id") == sid2]
        check("cancelada por admin aparece en aviso del estudiante", bool(rc))
        notifs = (await c.get("/student/notifications", headers=MH)).json()
        nitems = notifs.get("items", notifs) if isinstance(notifs, dict) else notifs
        nc = [n for n in nitems if "cancelada" in (n.get("title") or "").lower()]
        check("cancelada por admin genera notificación al estudiante", bool(nc))

        # 4. Calificar exige nota válida
        due = (now_utc + datetime.timedelta(days=2)).isoformat()
        ta = await c.post("/teacher/assignments", headers=TH, json={
            "title": "Sync Grade", "description": "t", "due_at_utc": due,
            "course_id": cid, "level_id": m_lvl["id"],
        })
        aid = ta.json()["id"]
        await c.post(f"/student/assignments/{aid}/submit", headers=MH, json={"content": "x"})
        subs = (await c.get(f"/teacher/assignments/{aid}/submissions", headers=TH)).json()
        subsl = subs.get("items", subs) if isinstance(subs, dict) else subs
        sub_id = subsl[0]["id"]
        bad = await c.post(f"/teacher/submissions/{sub_id}/grade", headers=TH, json={"nota": 90})
        check("calificar sin 'score' es rechazado (400)", bad.status_code == 400)
        good = await c.post(f"/teacher/submissions/{sub_id}/grade", headers=TH, json={"score": 88})
        check("calificar con score válido funciona", good.status_code == 200)

    print(f"\n{PASS}/{PASS+FAIL} tests pasaron")
    if FAIL:
        sys.exit(1)

asyncio.run(main())
