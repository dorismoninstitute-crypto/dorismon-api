"""V3.9.40 — Asistencia vista desde el admin y aviso a profesores."""
import sys
import asyncio
import datetime
import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
ADMIN = {"email": "admin@dorismon.do", "password": "DorismonAdmin2026!"}
STUDENT = {"email": "maria.estudiante@dorismon.do", "password": "Estudiante2026!"}
SEED_TEACHERS = ("ana@dorismon.do", "luis@dorismon.do", "sara@dorismon.do")


async def main():
    passed = total = 0

    def check(label, ok):
        nonlocal passed, total
        total += 1
        if ok:
            passed += 1
        print(f"  {'✓' if ok else '✗'} {label}")

    async with httpx.AsyncClient(base_url=BASE, timeout=30) as c:
        tok = (await c.post("/auth/login", json=ADMIN)).json()["access_token"]
        AH = {"Authorization": f"Bearer {tok}"}
        stok = (await c.post("/auth/login", json=STUDENT)).json()["access_token"]
        SH = {"Authorization": f"Bearer {stok}"}

        cid = (await c.get("/admin/courses", headers=AH)).json()[0]["id"]
        lv = (await c.get(f"/admin/levels-by-course/{cid}", headers=AH)).json()
        lvls = lv["items"] if isinstance(lv, dict) else lv
        mu = (await c.get("/admin/users?q=maria.estudiante", headers=AH)).json()
        est = mu["items"][0]["id"]
        perfil = (await c.get(f"/admin/students/{est}/profile", headers=AH)).json()
        lvl = [l for l in lvls if l["code"] == perfil.get("current_level_code")][0]
        profes = (await c.get("/admin/users?role=teacher", headers=AH)).json()
        profe = [p for p in profes["items"] if p["email"] in SEED_TEACHERS][0]
        ptok = (await c.post("/auth/login", json={"email": profe["email"],
                                                  "password": "Profe2026!"})).json()
        TH = {"Authorization": f"Bearer {ptok['access_token']}"}
        now = datetime.datetime.now(datetime.timezone.utc)

        # Una clase con lista y otra sin
        con = (await c.post("/admin/sessions", headers=AH, json={
            "title": "Test con lista",
            "starts_at_utc": (now - datetime.timedelta(days=1)).isoformat(),
            "ends_at_utc": (now - datetime.timedelta(days=1, hours=-1)).isoformat(),
            "modality": "online", "video_provider": "dorismon", "teacher_id": profe["id"],
            "course_id": cid, "level_id": lvl["id"],
        })).json()["id"]
        await c.post("/admin/sessions", headers=AH, json={
            "title": "Test sin lista",
            "starts_at_utc": (now - datetime.timedelta(days=2)).isoformat(),
            "ends_at_utc": (now - datetime.timedelta(days=2, hours=-1)).isoformat(),
            "modality": "online", "video_provider": "dorismon", "teacher_id": profe["id"],
            "course_id": cid, "level_id": lvl["id"],
        })

        att = (await c.get(f"/teacher/sessions/{con}/attendance", headers=TH)).json()
        alumnos = att.get("students", [])
        if alumnos:
            await c.post(f"/teacher/sessions/{con}/attendance", headers=TH, json={
                "records": [{"student_id": a["student_id"], "state": "present"}
                            for a in alumnos]})

        # ---------- La vista ----------
        ov = await c.get("/admin/attendance-overview?days=7", headers=AH)
        check("La vista de asistencia responde", ov.status_code == 200)
        data = ov.json()
        check("Trae el resumen con totales",
              "totals" in data and "classes" in data["totals"])
        check("Distingue clases con y sin lista",
              data["totals"].get("with_attendance", 0) >= 1
              and data["totals"].get("without_attendance", 0) >= 1)
        check("Calcula el promedio de asistencia",
              data["totals"].get("attendance_rate") is not None)
        check("Detecta qué profesores no pasaron lista",
              len(data.get("teachers_missing_attendance", [])) >= 1)

        conlista = [x for x in data["classes"] if x["has_attendance"]]
        check("Las clases con lista traen quiénes asistieron",
              bool(conlista) and bool(conlista[0].get("students")))
        check("Marca cuáles se pueden cobrar",
              all("billable" in x for x in data["classes"]))

        # ---------- El aviso ----------
        av = await c.post("/admin/remind-attendance", headers=AH,
                          json={"teacher_id": profe["id"], "days": 7})
        check("Se puede avisar a un profesor", av.status_code == 200)
        check("El aviso dice a cuántos se notificó",
              av.json().get("notified", 0) >= 1)

        notifs = (await c.get("/notifications", headers=TH)).json()
        ni = [x for x in (notifs.get("items", notifs) if isinstance(notifs, dict) else notifs)
              if isinstance(x, dict)]
        check("Al profesor le llega el aviso",
              any("sin pasar lista" in (x.get("title") or "").lower() for x in ni))

        todos = await c.post("/admin/remind-attendance", headers=AH, json={"days": 7})
        check("Se puede avisar a todos a la vez", todos.status_code == 200)

        # ---------- Seguridad ----------
        check("Un estudiante NO ve la asistencia general",
              (await c.get("/admin/attendance-overview", headers=SH)).status_code in (401, 403))
        check("Un profesor NO puede mandar los avisos",
              (await c.post("/admin/remind-attendance", headers=TH, json={})).status_code in (401, 403))
        check("Sin sesión no se accede",
              (await c.get("/admin/attendance-overview")).status_code in (401, 403))

        # ---------- V3.9.41: publicar / despublicar quizzes ----------
        ia = await c.post("/ai/quiz/create", headers=TH, json={
            "level_id": lvl["id"],
            "quiz": {"title": "Test Quiz Publicar", "description": "x",
                     "questions": [{"text": "I ___ to school",
                                    "options": ["go", "goes", "going", "gone"],
                                    "correct_index": 0, "explanation": "x"}]},
        })
        check("La IA crea el quiz sin publicar", ia.status_code == 200)
        if ia.status_code == 200:
            qid = ia.json().get("quiz_id")
            check("Queda en borrador al crearse", ia.json().get("published") is False)

            qz = (await c.get("/student/quizzes", headers=SH)).json()
            lst = qz.get("items", qz) if isinstance(qz, dict) else qz
            check("Sin publicar, el estudiante NO lo ve",
                  not any(x.get("id") == qid for x in lst))

            pub = await c.post(f"/teacher/quizzes/{qid}/publish", headers=TH)
            check("Se puede publicar", pub.status_code == 200)
            check("Al publicar se avisa a los estudiantes",
                  pub.json().get("notified", 0) >= 1)

            qz2 = (await c.get("/student/quizzes", headers=SH)).json()
            lst2 = qz2.get("items", qz2) if isinstance(qz2, dict) else qz2
            check("Publicado, el estudiante SÍ lo ve",
                  any(x.get("id") == qid for x in lst2))

            unpub = await c.post(f"/teacher/quizzes/{qid}/unpublish", headers=TH)
            check("Se puede despublicar", unpub.status_code == 200)
            qz3 = (await c.get("/student/quizzes", headers=SH)).json()
            lst3 = qz3.get("items", qz3) if isinstance(qz3, dict) else qz3
            check("Despublicado, deja de verlo",
                  not any(x.get("id") == qid for x in lst3))

    print(f"{passed}/{total} tests pasaron")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
