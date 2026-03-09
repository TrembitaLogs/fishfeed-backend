"""Web endpoints for deep link landing pages.

Serves HTML pages for /join/{code} URLs that attempt to open the mobile app
via App Links/Universal Links, with fallback to app store links.
"""

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter(tags=["web"])


@router.get("/join/{invite_code}", response_class=HTMLResponse)
async def join_landing_page(invite_code: str) -> HTMLResponse:
    """Landing page for family invite deep links.

    When a user opens https://api.fishfeed.club/join/CODE in a browser:
    - If app is installed and App Links verified: OS intercepts, app opens directly
    - If app is NOT installed: this page loads and tries custom scheme fallback
    """
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Join Family — FishFeed</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: linear-gradient(135deg, #0D47A1 0%, #1565C0 50%, #42A5F5 100%);
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            color: #fff;
        }}
        .card {{
            background: rgba(255, 255, 255, 0.12);
            backdrop-filter: blur(20px);
            border-radius: 24px;
            padding: 48px 32px;
            max-width: 400px;
            width: 90%;
            text-align: center;
            border: 1px solid rgba(255, 255, 255, 0.2);
        }}
        .icon {{
            font-size: 64px;
            margin-bottom: 16px;
        }}
        h1 {{
            font-size: 24px;
            font-weight: 700;
            margin-bottom: 8px;
        }}
        .subtitle {{
            font-size: 16px;
            opacity: 0.85;
            margin-bottom: 32px;
            line-height: 1.5;
        }}
        .btn {{
            display: inline-block;
            background: #fff;
            color: #0D47A1;
            font-size: 17px;
            font-weight: 600;
            padding: 14px 32px;
            border-radius: 14px;
            text-decoration: none;
            margin-bottom: 16px;
            width: 100%;
            transition: transform 0.15s;
        }}
        .btn:active {{ transform: scale(0.97); }}
        .stores {{
            display: flex;
            gap: 12px;
            justify-content: center;
            margin-top: 24px;
        }}
        .store-btn {{
            display: inline-flex;
            align-items: center;
            gap: 8px;
            background: rgba(255, 255, 255, 0.15);
            color: #fff;
            font-size: 14px;
            font-weight: 500;
            padding: 10px 18px;
            border-radius: 10px;
            text-decoration: none;
            border: 1px solid rgba(255, 255, 255, 0.25);
        }}
        .divider {{
            margin: 24px 0;
            opacity: 0.3;
            border-top: 1px solid #fff;
        }}
        .footer {{
            font-size: 13px;
            opacity: 0.6;
            margin-top: 24px;
        }}
    </style>
</head>
<body>
    <div class="card">
        <div class="icon">🐠</div>
        <h1>FishFeed</h1>
        <p class="subtitle">You've been invited to join a family aquarium!</p>

        <a href="fishfeed://join/{invite_code}" class="btn" id="openApp">
            Open in FishFeed
        </a>

        <div class="divider"></div>

        <p class="subtitle" style="margin-bottom: 16px; font-size: 14px;">
            Don't have the app yet?
        </p>
        <div class="stores">
            <a href="https://play.google.com/store/apps/details?id=com.fishfeed.fishfeed"
               class="store-btn">
                ▶ Google Play
            </a>
            <a href="https://apps.apple.com/app/fishfeed/id000000000"
               class="store-btn">
                 App Store
            </a>
        </div>

        <p class="footer">FishFeed — Smart Aquarium Care</p>
    </div>

    <script>
        // Try to open the app via custom scheme after a short delay.
        // If App Links/Universal Links worked, user is already in the app
        // and this page never fully loads.
        setTimeout(function() {{
            window.location.href = 'fishfeed://join/{invite_code}';
        }}, 300);
    </script>
</body>
</html>"""
    return HTMLResponse(content=html)
