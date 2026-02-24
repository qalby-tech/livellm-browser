import asyncio
from patchright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        context = await browser.new_context()
        await browser.close()
        try:
            await context.new_page()
        except Exception as e:
            print(f"Exception type: {type(e)}")
            print(f"Exception name: {type(e).__name__}")
            print(f"Exception module: {type(e).__module__}")
            print(f"Exception message: {e}")

asyncio.run(main())
