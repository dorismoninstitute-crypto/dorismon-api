"""V3.9.56 — REPETIR UN NIVEL Y DATOS LEGACY.

Dos escenarios que hasta ahora nadie probaba:

1. Juan cursa B1, lo completa, y VUELVE A MATRICULARSE en B1.
   La segunda matrícula debe empezar de cero: nada del progreso anterior
   puede aparecer como hecho.

2. Datos anteriores a estas versiones (sin `enrollment_id`) sobreviven a la
   migración y siguen contándose, sin inventarles matrícula.
"""
import sys
import asyncio
import datetime
import sqlite3
import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
ADMIN = {"email": "admin@dorismon.do", "password": "DorismonAdmin2026!"}
DB = "dorismon.db"


async def main():
    passed = total = 0

    def check(label, ok):
        nonlocal passed, total
        total += 1
        if ok:
            passed += 1
        print(f"  {'✓' if ok else '✗'} {label}")

    async with httpx.AsyncClient(base_url=BASE, timeout=40) as c:
        tok = (await c.post("/auth/login", json=ADMIN)).json()["access_token"]
        AH = {"Authorization": f"Bearer {tok}"}

        cursos = (await c.get("/admin/courses", headers=AH)).json()
        CA = cursos[0]
        lv = (await c.get(f"/admin/levels-by-course/{CA['id']}", headers=AH)).json()
        NIV = lv["items"] if isinstance(lv, dict) else lv
        if not NIV:
            print("  (sin niveles)")
            return 0
        B1 = NIV[0]

        profes = (await c.get("/admin/users?role=teacher", headers=AH)).json()
        seed = [p for p in profes["items"]
                if p["email"] in ("ana@dorismon.do", "luis@dorismon.do",
                                  "sara@dorismon.do")]
        if not seed:
            print("  (sin profesores)")
            return 0
        PROF = seed[0]

        # Estudiante propio
        await c.post("/admin/users", headers=AH, json={
            "email": "rep.juan@dorismon.do", "full_name": "REP Juan",
            "password": "Estudiante2026!", "role": "student"})
        b = (await c.get("/admin/users?q=rep.juan", headers=AH)).json()
        if not b.get("items"):
            print("  (no se creó el estudiante)")
            return 0
        JID = b["items"][0]["id"]
        jl = await c.post("/auth/login", json={"email": "rep.juan@dorismon.do",
                                                "password": "Estudiante2026!"})
        JH = {"Authorization": f"Bearer {jl.json()['access_token']}"}

        # Módulo con lecciones
        mods = (await c.get(f"/admin/levels/{B1['id']}/modules", headers=AH))
        lista = []
        if mods.status_code == 200:
            jm = mods.json()
            lista = jm.get("items", jm) if isinstance(jm, dict) else jm
        if not lista:
            print("  (el nivel no tiene módulos)")
            return 0
        MOD = lista[0]

        lec = await c.get(f"/admin/modules/{MOD['id']}/lessons", headers=AH)
        lecciones = []
        if lec.status_code == 200:
            jl2 = lec.json()
            lecciones = jl2.get("items", jl2) if isinstance(jl2, dict) else jl2
        if len(lecciones) < 2:
            for i in range(2):
                await c.post("/admin/lessons", headers=AH, json={
                    "module_id": MOD["id"], "title": f"REP Lección {i}",
                    "description": "x", "order_index": i})
            lec = await c.get(f"/admin/modules/{MOD['id']}/lessons", headers=AH)
            jl2 = lec.json() if lec.status_code == 200 else {}
            lecciones = jl2.get("items", jl2) if isinstance(jl2, dict) else jl2

        # ══════════════════════════════════════════════════════════════════
        # PRIMERA MATRÍCULA: estudia y completa
        # ══════════════════════════════════════════════════════════════════
        print("\n  --- Primera vez en B1 ---")
        r1 = await c.post("/admin/enrollments", headers=AH, json={
            "student_id": JID, "course_id": CA["id"],
            "level_id": B1["id"], "teacher_id": PROF["id"]})
        check("Se crea la primera matrícula", r1.status_code in (200, 201))
        ENR1 = r1.json().get("id") if r1.status_code in (200, 201) else None
        if not ENR1:
            print(f"{passed}/{total} tests pasaron")
            return 1

        # Cubre las lecciones
        for L in lecciones:
            await c.post(f"/lessons/{L['id']}/complete", headers=JH,
                         json={"completed": True})

        prog1 = (await c.get("/progress/my-course", headers=JH)).json()
        check("Su progreso de lecciones queda registrado",
              prog1.get("completed_modules") is not None)

        # Se marca completada
        cx = sqlite3.connect(DB)
        cx.execute("UPDATE enrollments SET academic_status='completed', "
                   "completed_at=datetime('now'), is_active=0 WHERE id=?", (ENR1,))
        cx.commit()
        n_lec1 = cx.execute(
            "SELECT COUNT(*) FROM lesson_progress WHERE enrollment_id=?",
            (ENR1,)).fetchone()[0]
        cx.close()
        check("Las lecciones quedaron ligadas a la PRIMERA matrícula",
              n_lec1 >= len(lecciones))

        # ══════════════════════════════════════════════════════════════════
        # SEGUNDA MATRÍCULA EN EL MISMO NIVEL: debe empezar de cero
        # ══════════════════════════════════════════════════════════════════
        print("\n  --- Repite B1 ---")
        r2 = await c.post("/admin/enrollments", headers=AH, json={
            "student_id": JID, "course_id": CA["id"],
            "level_id": B1["id"], "teacher_id": PROF["id"]})
        check("Se puede volver a matricular en el MISMO nivel",
              r2.status_code in (200, 201))
        ENR2 = r2.json().get("id") if r2.status_code in (200, 201) else None
        check("Es una matrícula distinta", ENR2 and ENR2 != ENR1)

        if ENR2:
            el2 = await c.get(f"/teacher/enrollments/{ENR2}/eligibility",
                              headers=AH)
            check("Se puede consultar la elegibilidad de la segunda",
                  el2.status_code == 200)
            if el2.status_code == 200:
                d = el2.json()
                mods_d = d.get("modules", {})
                check("La segunda matrícula NO hereda módulos completados",
                      mods_d.get("completed", 0) == 0)
                check("Ni las habilidades de la primera",
                      d["metrics"]["skills"] == {})
                check("Ni su asistencia",
                      d["metrics"]["attendance_pct"] is None
                      or d["metrics"]["attendance_detail"]["total_classes"] == 0)
                check("Y NO es elegible de entrada", d["eligible"] is False)

                # El detalle del módulo lo dice
                m0 = [x for x in mods_d.get("modules", [])
                      if x["module_id"] == MOD["id"]]
                check("El módulo aparece sin lecciones cubiertas",
                      bool(m0) and m0[0]["lessons_completed"] == 0)

            # Al completar de nuevo, se guarda en la SEGUNDA
            for L in lecciones:
                await c.post(f"/lessons/{L['id']}/complete", headers=JH,
                             json={"completed": True})
            cx2 = sqlite3.connect(DB)
            n1 = cx2.execute(
                "SELECT COUNT(*) FROM lesson_progress WHERE enrollment_id=?",
                (ENR1,)).fetchone()[0]
            n2 = cx2.execute(
                "SELECT COUNT(*) FROM lesson_progress WHERE enrollment_id=?",
                (ENR2,)).fetchone()[0]
            cx2.close()
            check("La primera matrícula CONSERVA su progreso", n1 >= len(lecciones))
            check("Y la segunda tiene el suyo, independiente",
                  n2 >= len(lecciones))

            # ── CERTIFICADO: hay que decir de cuál matrícula ──
            print("\n  --- Certificado con dos matrículas del mismo nivel ---")
            amb = await c.post("/admin/certificates", headers=AH, json={
                "student_id": JID, "course_id": CA["id"],
                "level_id": B1["id"], "hours": 120})
            check("Con dos matrículas, NO elige una en silencio",
                  amb.status_code == 400)
            check("Y pide que se indique cuál",
                  amb.status_code == 400
                  and "matrícula" in str(amb.json()).lower())

            cert = await c.post("/admin/certificates", headers=AH, json={
                "student_id": JID, "course_id": CA["id"],
                "level_id": B1["id"], "hours": 120,
                "enrollment_id": ENR1})
            check("Indicando la matrícula, se emite",
                  cert.status_code in (200, 201))
            if cert.status_code in (200, 201):
                cx3 = sqlite3.connect(DB)
                f = cx3.execute(
                    "SELECT enrollment_id FROM certificates WHERE student_id=? "
                    "ORDER BY rowid DESC LIMIT 1", (JID,)).fetchone()
                cx3.close()
                check("Y queda ligado a la matrícula indicada",
                      bool(f) and f[0] == ENR1)

        # ══════════════════════════════════════════════════════════════════
        # DATOS LEGACY: sin enrollment_id
        # ══════════════════════════════════════════════════════════════════
        print("\n  --- Datos anteriores a estas versiones ---")
        if lecciones and ENR2:
            cx4 = sqlite3.connect(DB)
            try:
                cx4.execute(
                    "INSERT INTO lesson_progress "
                    "(id, student_id, lesson_id, enrollment_id, is_completed, progress_pct) "
                    "VALUES (?, ?, ?, NULL, 1, 100)",
                    (f"legacy-{JID[:8]}", JID, lecciones[-1]["id"]))
                cx4.commit()
                ok_ins = True
            except Exception:
                ok_ins = False
            n_leg = cx4.execute(
                "SELECT COUNT(*) FROM lesson_progress WHERE enrollment_id IS NULL"
            ).fetchone()[0]
            cx4.close()
            check("Se admiten registros legacy sin matrícula", ok_ins)
            check("Y conviven con los nuevos", n_leg >= 1)

            el3 = await c.get(f"/teacher/enrollments/{ENR2}/eligibility",
                              headers=AH)
            check("El sistema sigue funcionando con datos legacy",
                  el3.status_code == 200)

        # ══════════════════════════════════════════════════════════════════
        # V3.9.57 §1 — DOS ModuleProgress DEL MISMO MÓDULO
        # ══════════════════════════════════════════════════════════════════
        print("\n  --- Progreso de módulo en dos matrículas ---")
        if ENR2:
            cxm = sqlite3.connect(DB)
            try:
                cxm.execute(
                    "INSERT INTO module_progress "
                    "(id, student_id, module_id, enrollment_id, status, "
                    "attended_count, quiz_passed) "
                    "VALUES (?, ?, ?, ?, 'completed', 3, 0)",
                    (f"mp1-{JID[:8]}", JID, MOD["id"], ENR1))
                cxm.execute(
                    "INSERT INTO module_progress "
                    "(id, student_id, module_id, enrollment_id, status, "
                    "attended_count, quiz_passed) "
                    "VALUES (?, ?, ?, ?, 'in_progress', 1, 0)",
                    (f"mp2-{JID[:8]}", JID, MOD["id"], ENR2))
                cxm.commit()
                coexisten = True
            except Exception as ex:
                coexisten = False
                print(f"     (falló: {ex})")
            n_mp = cxm.execute(
                "SELECT COUNT(*) FROM module_progress WHERE student_id=? AND module_id=?",
                (JID, MOD["id"])).fetchone()[0]
            cxm.close()
            check("Dos ModuleProgress del MISMO módulo pueden coexistir",
                  coexisten and n_mp >= 2)

            # Y cada matrícula ve el suyo
            pr2 = (await c.get("/progress/my-course", headers=JH)).json()
            check("El progreso mostrado es el de la matrícula activa",
                  pr2.get("completed_modules", 0) == 0)

        # ══════════════════════════════════════════════════════════════════
        # V3.9.57 §5 — EL LEGACY NO COMPLETA UNA MATRÍCULA MODERNA
        # ══════════════════════════════════════════════════════════════════
        print("\n  --- El legacy no contamina ---")
        if ENR2 and lecciones:
            LX = lecciones[0]
            cxl = sqlite3.connect(DB)
            # Se borra el progreso moderno de esa lección en #2 y se deja
            # SOLO un registro legacy completado
            # Se limpia TODO el progreso moderno de #2, no solo el de esta
            # lección: si queda otro, el módulo tendría cobertura legítima y
            # no probaríamos el aislamiento del legacy.
            cxl.execute("DELETE FROM lesson_progress WHERE enrollment_id=?", (ENR2,))
            cxl.execute("DELETE FROM lesson_progress WHERE enrollment_id IS NULL AND lesson_id=?",
                        (LX["id"],))
            cxl.execute(
                "INSERT INTO lesson_progress "
                "(id, student_id, lesson_id, enrollment_id, is_completed, progress_pct) "
                "VALUES (?, ?, ?, NULL, 1, 100)",
                (f"leg2-{JID[:8]}", JID, LX["id"]))
            cxl.commit(); cxl.close()

            det = await c.get(f"/lessons/{LX['id']}", headers=JH)
            if det.status_code == 200:
                check("El panel de #2 NO muestra la lección legacy como hecha",
                      det.json().get("progress", {}).get("is_completed") is False)

            el_leg = await c.get(f"/teacher/enrollments/{ENR2}/eligibility",
                                 headers=AH)
            if el_leg.status_code == 200:
                dm = el_leg.json().get("modules", {})
                m_leg = [x for x in dm.get("modules", [])
                         if x["module_id"] == MOD["id"]]
                check("El módulo NO gana cobertura por el legacy",
                      bool(m_leg) and m_leg[0]["lessons_completed"] == 0)
                check("La elegibilidad de #2 no mejora por el legacy",
                      el_leg.json()["eligible"] is False)

            cur = (await c.get("/student/courses", headers=JH)).json()
            ci = cur.get("items", cur) if isinstance(cur, dict) else cur
            mias_c = [x for x in ci if x.get("enrollment_id") == ENR2]
            check("El dashboard de #2 no cuenta la lección legacy",
                  not mias_c or mias_c[0].get("completed_lessons", 0) == 0)

            # Ahora Juan la completa DENTRO de #2
            r_comp = await c.post(f"/lessons/{LX['id']}/complete", headers=JH,
                                  json={"completed": True})
            check("Puede completarla en #2 sin violar UNIQUE",
                  r_comp.status_code == 200)

            cxv = sqlite3.connect(DB)
            n_leg = cxv.execute(
                "SELECT COUNT(*) FROM lesson_progress "
                "WHERE lesson_id=? AND enrollment_id IS NULL", (LX["id"],)).fetchone()[0]
            n_mod = cxv.execute(
                "SELECT COUNT(*) FROM lesson_progress "
                "WHERE lesson_id=? AND enrollment_id=?", (LX["id"], ENR2)).fetchone()[0]
            cxv.close()
            check("Se creó un registro propio de #2", n_mod == 1)
            check("Y el legacy sigue INTACTO con enrollment_id NULL", n_leg == 1)

        # ══════════════════════════════════════════════════════════════════
        # V3.9.57 §8-§10 — INTEGRIDAD DEL CERTIFICADO
        # ══════════════════════════════════════════════════════════════════
        print("\n  --- Integridad del certificado ---")
        if ENR2:
            cursos_t = (await c.get("/admin/courses", headers=AH)).json()
            otro = [x for x in cursos_t if x["id"] != CA["id"]]
            if otro:
                lvo = (await c.get(f"/admin/levels-by-course/{otro[0]['id']}",
                                   headers=AH)).json()
                NO_ = lvo["items"] if isinstance(lvo, dict) else lvo
                if NO_:
                    cruz = await c.post("/admin/certificates", headers=AH, json={
                        "student_id": JID, "course_id": otro[0]["id"],
                        "level_id": NO_[0]["id"], "hours": 120,
                        "enrollment_id": ENR1})
                    check("Matrícula de un curso + cuerpo de otro → rechazado",
                          cruz.status_code == 400)
                    check("Y dice que la matrícula no es de ese curso",
                          cruz.status_code == 400
                          and "curso" in str(cruz.json()).lower())

            # §9: #1 completada se valida aunque #2 esté activa
            cxc = sqlite3.connect(DB)
            cxc.execute("DELETE FROM certificates WHERE enrollment_id=?", (ENR1,))
            cxc.commit(); cxc.close()

            ok1 = await c.post("/admin/certificates", headers=AH, json={
                "student_id": JID, "course_id": CA["id"],
                "level_id": B1["id"], "hours": 120, "enrollment_id": ENR1})
            check("Certificar #1 (completada) se permite aunque #2 esté activa",
                  ok1.status_code in (200, 201))

            # §9: #2 activa se rechaza
            mal2 = await c.post("/admin/certificates", headers=AH, json={
                "student_id": JID, "course_id": CA["id"],
                "level_id": B1["id"], "hours": 120, "enrollment_id": ENR2})
            check("Certificar #2 (activa) se rechaza sin excepción",
                  mal2.status_code == 409)
            check("Y el mensaje habla de ESA matrícula",
                  mal2.status_code == 409
                  and mal2.json().get("detail", {}).get("enrollment_id") == ENR2)

            # §12: la duplicidad es por matrícula
            dup1 = await c.post("/admin/certificates", headers=AH, json={
                "student_id": JID, "course_id": CA["id"],
                "level_id": B1["id"], "hours": 120, "enrollment_id": ENR1})
            check("No deja dos certificados activos de la MISMA matrícula",
                  dup1.status_code == 400)

            # §11: el historial no muestra el de #1 en #2
            prog_j = (await c.get("/student/my-progress", headers=JH)).json()
            h1 = [x for x in prog_j.get("history", [])
                  if x["enrollment_id"] == ENR1]
            a2 = [x for x in prog_j.get("active", [])
                  if x["enrollment_id"] == ENR2]
            check("El historial liga el certificado SOLO a #1",
                  bool(h1) and h1[0].get("certificate_code") is not None)
            check("Y #2 aparece sin certificado",
                  bool(a2) and a2[0].get("certificate_code") is None)

            # §10: la excepción usa la matrícula resuelta
            exc2 = await c.post("/admin/certificates", headers=AH, json={
                "student_id": JID, "course_id": CA["id"],
                "level_id": B1["id"], "hours": 120, "enrollment_id": ENR2,
                "confirmar_incompleto": True,
                "exception_reason": "Caso especial aprobado por Dirección"})
            check("Con motivo, la excepción emite para #2",
                  exc2.status_code in (200, 201))
            if exc2.status_code in (200, 201):
                cxa = sqlite3.connect(DB)
                f = cxa.execute(
                    "SELECT enrollment_id FROM certificates "
                    "WHERE student_id=? ORDER BY rowid DESC LIMIT 1",
                    (JID,)).fetchone()
                aud = cxa.execute(
                    "SELECT target_id FROM audit_logs "
                    "WHERE action='certificate_exception' "
                    "ORDER BY rowid DESC LIMIT 1").fetchone()
                cxa.close()
                check("El certificado queda ligado a #2", bool(f) and f[0] == ENR2)
                check("Y la auditoría registra ESA matrícula",
                      bool(aud) and aud[0] == ENR2)

    print(f"{passed}/{total} tests pasaron")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
