import asyncio
from app.database import AsyncSessionLocal
from app.services.zoho_service import sync_recent_zoho_leads

async def main():
    async with AsyncSessionLocal() as db:
        try:
            result = await sync_recent_zoho_leads(db)
            print("Success:", result)
        except Exception as e:
            import traceback
            traceback.print_exc()

asyncio.run(main())
