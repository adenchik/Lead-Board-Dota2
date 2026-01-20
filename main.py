import asyncio
import time
from contextlib import asynccontextmanager

import httpx
import aiosqlite
from iso3166 import countries_by_alpha2
from fastapi import FastAPI, Request, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

DB_PATH = "leaderboard.db"
REGIONS = ["americas", "europe", "se_asia", "china"]
API_URL = "https://www.dota2.com/webapi/ILeaderboard/GetDivisionLeaderboard/v0001"


async def init_db():
    """Initialize SQLite database."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS players (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                region TEXT NOT NULL,
                rank INTEGER NOT NULL,
                name TEXT NOT NULL,
                team_id INTEGER,
                team_tag TEXT,
                sponsor TEXT,
                country TEXT,
                UNIQUE(region, rank)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value INTEGER
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_region ON players(region)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_country ON players(country)")
        await db.commit()


async def fetch_leaderboards() -> dict | None:
    """Fetch leaderboard data from Dota 2 API."""
    database = {}
    latest_time_posted = 0
    latest_next_update = 0

    async with httpx.AsyncClient(timeout=30) as client:
        tasks = [
            client.get(API_URL, params={"division": region, "leaderboard": 0})
            for region in REGIONS
        ]
        responses = await asyncio.gather(*tasks, return_exceptions=True)

    for region, response in zip(REGIONS, responses):
        if isinstance(response, Exception):
            print(f"Error fetching {region}: {response}")
            continue

        data = response.json()
        leaderboard = data.get("leaderboard", [])
        for idx, player in enumerate(leaderboard, start=1):
            player["rank"] = idx
        database[region] = leaderboard

        tp = data.get("time_posted", 0)
        np = data.get("next_scheduled_post_time", 0)
        latest_time_posted = max(latest_time_posted, tp)
        latest_next_update = max(latest_next_update, np)

    if not database:
        return None

    database["time_posted"] = latest_time_posted
    database["next_scheduled_post_time"] = latest_next_update
    return database


async def save_to_db(data: dict):
    """Save leaderboard data to SQLite."""
    async with aiosqlite.connect(DB_PATH) as db:
        for region in REGIONS:
            players = data.get(region, [])
            if not players:
                continue

            await db.execute("DELETE FROM players WHERE region = ?", (region,))

            await db.executemany(
                """INSERT INTO players (region, rank, name, team_id, team_tag, sponsor, country)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        region,
                        p.get("rank"),
                        p.get("name", ""),
                        p.get("team_id"),
                        p.get("team_tag"),
                        p.get("sponsor"),
                        p.get("country"),
                    )
                    for p in players
                ],
            )

        await db.execute(
            "INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)",
            ("time_posted", data.get("time_posted", 0)),
        )
        await db.execute(
            "INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)",
            ("next_scheduled_post_time", data.get("next_scheduled_post_time", 0)),
        )
        await db.commit()


async def get_players(
    region: str,
    rank_from: int | None = None,
    rank_to: int | None = None,
    countries: list[str] | None = None,
    team: str | None = None,
    name_player: str | None = None,
) -> list[dict]:
    """Get players from database with filters."""
    query = "SELECT rank, name, team_id, team_tag, sponsor, country FROM players WHERE region = ?"
    params: list = [region]

    if rank_from and rank_to:
        query += " AND rank BETWEEN ? AND ?"
        params.extend([rank_from, rank_to])

    if countries:
        placeholders = ",".join("?" * len(countries))
        query += f" AND UPPER(country) IN ({placeholders})"
        params.extend(countries)

    if team == "yes":
        query += " AND team_tag IS NOT NULL AND team_tag != ''"
    elif team == "no":
        query += " AND (team_tag IS NULL OR team_tag = '')"

    if name_player:
        query += " AND LOWER(name) LIKE ?"
        params.append(f"{name_player.lower()}%")

    query += " ORDER BY rank"

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


async def get_metadata() -> dict:
    """Get metadata from database."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT key, value FROM metadata") as cursor:
            rows = await cursor.fetchall()
            return {row[0]: row[1] for row in rows}


async def get_countries(region: str) -> dict[str, str]:
    """Get unique countries for a region."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT DISTINCT UPPER(country) FROM players WHERE region = ? AND country IS NOT NULL",
            (region,),
        ) as cursor:
            codes = [row[0] for row in await cursor.fetchall()]

    countries_full = {}
    for code in codes:
        country = countries_by_alpha2.get(code)
        if country:
            countries_full[code] = country.name
        else:
            countries_full[code] = "Unknown"

    return dict(sorted(countries_full.items(), key=lambda x: x[1]))


async def scheduled_task():
    """Background task to update data on schedule."""
    while True:
        try:
            data = await fetch_leaderboards()
            if data:
                await save_to_db(data)
                print(f"Data updated: {time.strftime('%H:%M:%S')}")

                next_update = data.get("next_scheduled_post_time", 0)
                now = int(time.time())

                if next_update > now:
                    sleep_for = next_update - now
                    print(f"Next update in {sleep_for // 60} minutes")
                    await asyncio.sleep(sleep_for)
                else:
                    await asyncio.sleep(3600)
            else:
                await asyncio.sleep(300)
        except Exception as e:
            print(f"Error updating data: {e}")
            await asyncio.sleep(60)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    await init_db()
    task = asyncio.create_task(scheduled_task())
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


@app.get("/", response_class=RedirectResponse)
async def default_region():
    return RedirectResponse(url="/europe")


@app.get("/{region}", response_class=HTMLResponse)
async def read_root(
    request: Request,
    region: str,
    rank_from: int | None = Query(None),
    rank_to: int | None = Query(None),
    countries: str | None = Query(None),
    team: str | None = Query(None),
    name_player: str | None = Query(None),
):
    if region not in REGIONS:
        return RedirectResponse(url="/europe")

    selected_countries = countries.split(",") if countries else []

    players = await get_players(
        region=region,
        rank_from=rank_from,
        rank_to=rank_to,
        countries=selected_countries if selected_countries else None,
        team=team,
        name_player=name_player,
    )

    metadata = await get_metadata()
    countries_list = await get_countries(region)

    return templates.TemplateResponse(
        request=request,
        name="main/main.html",
        context={
            "region": region,
            "data": players,
            "last_update": metadata.get("time_posted"),
            "next_update": metadata.get("next_scheduled_post_time"),
            "rank_from": rank_from,
            "rank_to": rank_to,
            "team": team,
            "name_player": name_player,
            "countries": countries_list,
            "selected_countries": selected_countries,
        },
    )
