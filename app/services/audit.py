"""Servicio de auditoría — registra acciones críticas."""
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.placement_booking import AuditLog


async def log_action(
    db: AsyncSession,
    user_id: str | None,
    action: str,
    module: str,
    target_id: str | None = None,
    ip: str | None = None,
    details: str | None = None,
    commit: bool = False,
):
    """Registra una acción auditable. NO hace commit por defecto."""
    log = AuditLog(
        user_id=user_id, action=action, module=module,
        target_id=target_id, ip=ip, details=details,
    )
    db.add(log)
    if commit:
        await db.commit()
