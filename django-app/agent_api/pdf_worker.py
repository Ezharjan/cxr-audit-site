"""Isolated, time-bounded Chromium renderer; reads trusted HTML, writes PDF bytes."""
import asyncio
import sys

from playwright.async_api import async_playwright


async def render(html):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(chromium_sandbox=True, timeout=15000)
        try:
            context = await browser.new_context(java_script_enabled=False, service_workers="block")
            await context.route("**/*", lambda route: route.abort())
            page = await context.new_page()
            await page.set_content(html, wait_until="load", timeout=10000)
            await page.emulate_media(media="print")
            return await asyncio.wait_for(page.pdf(format="A4", print_background=True,
                margin={"top": "12mm", "bottom": "12mm", "left": "12mm", "right": "12mm"}), timeout=15)
        finally:
            await browser.close()


if __name__ == "__main__":
    html = sys.stdin.buffer.read().decode("utf-8")
    sys.stdout.buffer.write(asyncio.run(render(html)))
