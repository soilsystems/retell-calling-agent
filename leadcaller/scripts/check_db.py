import asyncio
from sqlalchemy import select
from app.database import AsyncSessionLocal
from app.models import CallAttempt, CallJob, Lead, WhatsAppLog

async def main():
    async with AsyncSessionLocal() as session:
        # Get latest leads
        leads_res = await session.execute(select(Lead).order_by(Lead.id).limit(5))
        leads = leads_res.scalars().all()
        print("LEADS:")
        for l in leads:
            print(f"ID={l.id} Name={l.name} Phone={l.phone}")
        
        # Get latest call jobs
        jobs_res = await session.execute(select(CallJob).order_by(CallJob.scheduled_at.desc()).limit(5))
        jobs = jobs_res.scalars().all()
        print("\nCALL JOBS:")
        for j in jobs:
            print(f"ID={j.id} LeadId={j.lead_id} Status={j.status} ScheduledAt={j.scheduled_at}")

        # Get latest attempts
        attempts_res = await session.execute(select(CallAttempt).order_by(CallAttempt.started_at.desc()).limit(5))
        attempts = attempts_res.scalars().all()
        print("\nCALL ATTEMPTS:")
        for a in attempts:
            print(f"ID={a.id} JobId={a.call_job_id} Status={a.status} RetellId={a.retell_call_id}")

        # Get latest whatsapp logs
        wa_res = await session.execute(select(WhatsAppLog).order_by(WhatsAppLog.sent_at.desc()).limit(5))
        wa_logs = wa_res.scalars().all()
        print("\nWHATSAPP LOGS:")
        for w in wa_logs:
            print(f"ID={w.id} LeadId={w.lead_id} Status={w.status} Template={w.template_name} Error={w.error_message}")

if __name__ == "__main__":
    asyncio.run(main())
