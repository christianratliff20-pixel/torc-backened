"""
ROUTES — every non-auth endpoint in one file:
feed, coach, macros, workouts, vitals, community.
All mounted under different prefixes from main.py.
"""
from fastapi import APIRouter, Depends, HTTPException, Form, UploadFile, File
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime, timedelta
from anthropic import Anthropic
import mux_python

from database import get_db
from models import (
    FeedItem, User, MacroLog, WorkoutLog, VitalLog,
    Group, GroupMessage, Challenge, Comment, Collection, BodyMetrics, CoachMessage,
)
from auth import get_current_user_id
from helpers import generate_id
from moderation import run_moderation_pipeline
from config import settings

client = Anthropic()

# ── Mux client setup ──────────────────────────────────────────────────────
_mux_config = mux_python.Configuration()
_mux_config.username = settings.mux_token_id
_mux_config.password = settings.mux_token_secret
_mux_api_client = mux_python.ApiClient(_mux_config)
mux_uploads_api = mux_python.DirectUploadsApi(_mux_api_client)
mux_assets_api = mux_python.AssetsApi(_mux_api_client)

def mux_configured() -> bool:
    return bool(settings.mux_token_id and settings.mux_token_secret)

# One router per feature — main.py mounts each at its own prefix
feed_router = APIRouter()
coach_router = APIRouter()
macros_router = APIRouter()
workouts_router = APIRouter()
vitals_router = APIRouter()
body_router = APIRouter()
community_router = APIRouter()


def _resolve_user(db: Session, user_id: str) -> User:
    """Looks up the authenticated user or 404s. No bypass."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


# ═══════════════════════════════════════════════════════════════════════
# FEED
# ═══════════════════════════════════════════════════════════════════════

@feed_router.get("")
def get_feed(category: str = None, limit: int = 20, offset: int = 0, db: Session = Depends(get_db)):
    query = db.query(FeedItem).filter(FeedItem.moderation_status == "live")
    if category:
        query = query.filter(FeedItem.category == category)

    total = query.count()
    items = query.order_by(FeedItem.created_at.desc()).limit(limit).offset(offset).all()

    result = []
    for item in items:
        author = db.query(User).filter(User.id == item.user_id).first()
        result.append({
            "id": item.id,
            "type": item.type,
            "title": item.title,
            "description": item.description,
            "category": item.category,
            "author": {
                "id": author.id, "username": author.username,
                "is_creator": author.is_creator, "is_coach": author.is_coach,
            } if author else None,
            "engagement": {"likes": item.likes, "comments": item.comments, "saves": item.saves, "shares": item.shares},
            "macros": item.macros,
            "tags": item.tags,
            "created_at": item.created_at.isoformat(),
            "moderation_status": item.moderation_status,
            "video_status": item.video_status,
            "mux_playback_id": item.mux_playback_id,
        })

    return {"items": result, "total": total, "limit": limit, "offset": offset}


@feed_router.post("/upload-url")
def create_video_upload_url(user_id: str = Depends(get_current_user_id)):
    """
    Step 1 of video upload: ask Mux for a signed direct-upload URL.
    The frontend PUTs the raw video file straight to that URL (never through
    our backend — API keys never touch the browser, and we're not proxying
    potentially huge video files through our own server).
    """
    if not mux_configured():
        raise HTTPException(status_code=503, detail="Video upload isn't configured yet (missing Mux credentials)")

    try:
        asset_settings = mux_python.CreateAssetRequest(playback_policy=["public"])
        upload_request = mux_python.CreateUploadRequest(
            cors_origin="*",  # tighten to your real frontend domain once live
            new_asset_settings=asset_settings,
        )
        response = mux_uploads_api.create_direct_upload(upload_request)
        upload = response.data
        return {"upload_id": upload.id, "upload_url": upload.url}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Mux upload creation failed: {e}")


@feed_router.get("/upload-status/{upload_id}")
def get_video_upload_status(upload_id: str, user_id: str = Depends(get_current_user_id)):
    """
    Step 3: poll this after the browser finishes PUTting the file to Mux,
    to find out the asset_id once Mux has picked up the upload.
    """
    if not mux_configured():
        raise HTTPException(status_code=503, detail="Video upload isn't configured yet (missing Mux credentials)")

    try:
        response = mux_uploads_api.get_direct_upload(upload_id)
        upload = response.data
        return {"status": upload.status, "asset_id": upload.asset_id}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Mux status check failed: {e}")


@feed_router.get("/{item_id}/video-status")
def get_feed_item_video_status(item_id: str, db: Session = Depends(get_db)):
    """
    Step 4: poll this to find out when transcoding is done and get the
    real playback_id, which is what's needed to actually render the video.
    """
    item = db.query(FeedItem).filter(FeedItem.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    if not item.mux_asset_id:
        return {"video_status": item.video_status, "playback_id": None}

    if not mux_configured():
        return {"video_status": item.video_status, "playback_id": item.mux_playback_id}

    try:
        response = mux_assets_api.get_asset(item.mux_asset_id)
        asset = response.data
        item.video_status = asset.status  # preparing, ready, errored

        if asset.status == "ready" and asset.playback_ids:
            item.mux_playback_id = asset.playback_ids[0].id
            db.commit()

        return {"video_status": item.video_status, "playback_id": item.mux_playback_id}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Mux asset check failed: {e}")


@feed_router.post("/upload")
async def upload_content(
    title: str = Form(...),
    description: str = Form(...),
    category: str = Form(...),
    sub_category: str = Form(...),
    tags: str = Form(""),
    mux_upload_id: str = Form(None),  # from step 1, if this post has a video attached
    db: Session = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    user = _resolve_user(db, user_id)

    moderation = await run_moderation_pipeline(title, description, category, sub_category)

    item_id = generate_id("feed")
    moderation_status = "live" if moderation["pass"] else "under_review"

    video_status = "none"
    mux_asset_id = None
    if mux_upload_id and mux_configured():
        # Video was uploaded via the 2-step flow — look up its asset_id now.
        try:
            response = mux_uploads_api.get_direct_upload(mux_upload_id)
            mux_asset_id = response.data.asset_id
            video_status = "waiting" if not mux_asset_id else "processing"
        except Exception:
            video_status = "errored"

    feed_item = FeedItem(
        id=item_id,
        user_id=user.id,
        type="video" if mux_upload_id else "fact",
        title=title,
        description=description,
        category=category,
        workout_type=sub_category if category == "workout" else None,
        food_type=sub_category if category == "food" else None,
        tags=[t.strip() for t in tags.split(",") if t.strip()],
        moderation_status=moderation_status,
        moderation_notes=moderation.get("reason", ""),
        mux_upload_id=mux_upload_id,
        mux_asset_id=mux_asset_id,
        video_status=video_status,
    )
    db.add(feed_item)
    db.commit()
    db.refresh(feed_item)

    return {
        "id": item_id,
        "moderation_status": moderation_status,
        "video_status": video_status,
        "message": "Posted!" if moderation["pass"] else "Under review",
    }


@feed_router.post("/{item_id}/like")
def like_item(item_id: str, db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    item = db.query(FeedItem).filter(FeedItem.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    item.likes += 1
    db.commit()
    return {"likes": item.likes}


@feed_router.post("/{item_id}/save")
def save_item(item_id: str, db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    item = db.query(FeedItem).filter(FeedItem.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    item.saves += 1
    db.commit()
    return {"saves": item.saves}


class CommentRequest(BaseModel):
    text: str

@feed_router.get("/{item_id}/comments")
def get_comments(item_id: str, db: Session = Depends(get_db)):
    """Real comments for a feed item. Empty list if none — never fake data."""
    comments = db.query(Comment).filter(Comment.feed_item_id == item_id).order_by(Comment.created_at.desc()).all()
    result = []
    for c in comments:
        author = db.query(User).filter(User.id == c.user_id).first()
        result.append({
            "id": c.id,
            "text": c.text,
            "likes": c.likes,
            "author": author.username if author else "unknown",
            "created_at": c.created_at.isoformat(),
        })
    return {"comments": result}


@feed_router.post("/{item_id}/comments")
def post_comment(item_id: str, request: CommentRequest, db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    user = _resolve_user(db, user_id)
    item = db.query(FeedItem).filter(FeedItem.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    if not request.text.strip():
        raise HTTPException(status_code=400, detail="Comment cannot be empty")

    comment = Comment(id=generate_id("comment"), feed_item_id=item_id, user_id=user.id, text=request.text.strip())
    db.add(comment)
    item.comments += 1
    db.commit()
    db.refresh(comment)

    return {
        "id": comment.id,
        "text": comment.text,
        "likes": comment.likes,
        "author": user.username,
        "created_at": comment.created_at.isoformat(),
    }


# ═══════════════════════════════════════════════════════════════════════
# COLLECTIONS
# ═══════════════════════════════════════════════════════════════════════

class CollectionCreateRequest(BaseModel):
    name: str

@feed_router.get("/collections")
def list_collections(db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    """Real list of the current user's collections. Empty list if none."""
    user = _resolve_user(db, user_id)
    collections = db.query(Collection).filter(Collection.user_id == user.id).order_by(Collection.created_at.desc()).all()
    return {
        "collections": [
            {"id": c.id, "name": c.name, "item_count": len(c.items or [])}
            for c in collections
        ]
    }


@feed_router.post("/collections")
def create_collection(request: CollectionCreateRequest, db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    user = _resolve_user(db, user_id)
    if not request.name.strip():
        raise HTTPException(status_code=400, detail="Collection name cannot be empty")

    collection = Collection(id=generate_id("collection"), user_id=user.id, name=request.name.strip(), items=[])
    db.add(collection)
    db.commit()
    db.refresh(collection)
    return {"id": collection.id, "name": collection.name, "item_count": 0}


@feed_router.post("/collections/{collection_id}/items/{item_id}")
def add_item_to_collection(collection_id: str, item_id: str, db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    user = _resolve_user(db, user_id)
    collection = db.query(Collection).filter(Collection.id == collection_id, Collection.user_id == user.id).first()
    if not collection:
        raise HTTPException(status_code=404, detail="Collection not found")

    item = db.query(FeedItem).filter(FeedItem.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Feed item not found")

    items = list(collection.items or [])
    if item_id not in items:
        items.append(item_id)
        collection.items = items
        item.saves += 1
        db.commit()

    return {"id": collection.id, "name": collection.name, "item_count": len(collection.items)}


# ═══════════════════════════════════════════════════════════════════════
# COACH
# ═══════════════════════════════════════════════════════════════════════

class CoachMessageRequest(BaseModel):
    message: str

COACH_HISTORY_LIMIT = 40  # most recent messages kept in context — bounds token growth over months of use

@coach_router.post("/message")
def coach_message(request: CoachMessageRequest, db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    user = _resolve_user(db, user_id)
    today = datetime.utcnow().strftime("%Y-%m-%d")
    week_ago = (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%d")

    # ── Today's macros ──
    macro_log = db.query(MacroLog).filter(MacroLog.user_id == user.id, MacroLog.log_date == today).first()
    logged = {
        "calories": macro_log.calories if macro_log else 0,
        "protein": macro_log.protein if macro_log else 0,
        "carbs": macro_log.carbs if macro_log else 0,
        "fat": macro_log.fat if macro_log else 0,
    }
    goals = {"calories": user.goal_calories, "protein": user.goal_protein, "carbs": user.goal_carbs, "fat": user.goal_fat}

    # ── Recent workout history (last 7 days) ──
    recent_workouts = db.query(WorkoutLog).filter(
        WorkoutLog.user_id == user.id, WorkoutLog.log_date >= week_ago
    ).order_by(WorkoutLog.log_date.desc()).all()
    workout_lines = [
        f"  - {w.log_date}: {w.name} ({w.duration}min, energy {w.energy_level}/5)" for w in recent_workouts
    ] or ["  (none logged this week)"]

    # ── Body metrics trend ──
    body_entries = db.query(BodyMetrics).filter(BodyMetrics.user_id == user.id).order_by(BodyMetrics.metric_date.desc()).limit(2).all()
    body_line = "  (no body metrics logged yet)"
    if body_entries:
        latest = body_entries[0]
        body_line = f"  Latest: weight {latest.weight or '—'}lbs, body fat {latest.body_fat or '—'}%"
        if len(body_entries) > 1:
            prev = body_entries[1]
            if latest.weight and prev.weight:
                delta = round(latest.weight - prev.weight, 1)
                body_line += f" (weight change since last entry: {'+' if delta > 0 else ''}{delta}lbs)"

    # ── Today's vitals ──
    vital = db.query(VitalLog).filter(VitalLog.user_id == user.id, VitalLog.log_date == today).first()
    vitals_line = "  (not logged today)"
    if vital:
        vitals_line = f"  Water {vital.water}oz, sleep {vital.sleep}hrs, energy {vital.energy_level}/5, mood {vital.mood}/5"

    # ── Persisted conversation history (most recent N messages, oldest first) ──
    history = db.query(CoachMessage).filter(CoachMessage.user_id == user.id).order_by(
        CoachMessage.created_at.desc()
    ).limit(COACH_HISTORY_LIMIT).all()
    history = list(reversed(history))  # chronological order for the model

    system_prompt = f"""You are Apex AI Coach — direct, data-driven, no fluff.
User preference: "{user.coach_personality}"

Today's macros:
- Calories: {logged['calories']}/{goals['calories']} (remaining: {goals['calories'] - logged['calories']})
- Protein: {logged['protein']}g (goal: {goals['protein']}g)
- Carbs: {logged['carbs']}g (goal: {goals['carbs']}g)
- Fat: {logged['fat']}g (goal: {goals['fat']}g)

This week's workouts:
{chr(10).join(workout_lines)}

Body metrics:
{body_line}

Today's vitals:
{vitals_line}

You have access to the full conversation history below — use it. Don't repeat advice you've
already given, reference things the user has told you before when relevant, and notice patterns
across time (e.g. "you mentioned feeling low energy on leg days three times this month").

Respond in 2-4 sentences. Be specific to their actual data. No generic advice."""

    # Build the messages array from persisted history + the new message
    claude_messages = [{"role": ("user" if h.role == "user" else "assistant"), "content": h.text} for h in history]
    claude_messages.append({"role": "user", "content": request.message})

    try:
        response = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=1000,
            system=system_prompt,
            messages=claude_messages,
        )
        reply_text = response.content[0].text

        # Persist both turns so memory survives across sessions/devices
        db.add(CoachMessage(id=generate_id("coachmsg"), user_id=user.id, role="user", text=request.message))
        db.add(CoachMessage(id=generate_id("coachmsg"), user_id=user.id, role="coach", text=reply_text))
        db.commit()

        return {"response": reply_text}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Coach service error: {e}")


@coach_router.get("/history")
def get_coach_history(db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    """Full persisted conversation, so the frontend can restore it on load — real memory, not session-only state."""
    user = _resolve_user(db, user_id)
    history = db.query(CoachMessage).filter(CoachMessage.user_id == user.id).order_by(CoachMessage.created_at.asc()).all()
    return {
        "messages": [{"role": h.role, "text": h.text, "created_at": h.created_at.isoformat()} for h in history]
    }


@coach_router.post("/analyze-food-photo")
async def analyze_food_photo(
    photo: UploadFile = File(...),
    description: str = Form(""),  # optional user-provided context, e.g. "grilled chicken bowl, no rice"
    user_id: str = Depends(get_current_user_id),
):
    """
    Real photo-based food logging: user takes/uploads a photo, Claude's
    vision model estimates the meal identity and macros. Returns a
    structured estimate the user can review/edit before it's actually
    logged via the existing /api/macros/log endpoint — this never writes
    to the database itself, since an AI estimate should be a suggestion,
    not an automatic log entry.
    """
    import base64, json as json_module

    contents = await photo.read()
    if len(contents) > 8 * 1024 * 1024:  # 8MB safety cap
        raise HTTPException(status_code=400, detail="Photo is too large (max 8MB)")

    media_type = photo.content_type or "image/jpeg"
    if media_type not in ("image/jpeg", "image/png", "image/gif", "image/webp"):
        raise HTTPException(status_code=400, detail=f"Unsupported image type: {media_type}")

    image_b64 = base64.standard_b64encode(contents).decode("utf-8")

    context_line = f"\nAdditional context from the user: {description}" if description.strip() else ""

    try:
        response = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=800,
            system=(
                "You are a nutrition estimation assistant. Given a photo of food, identify what it "
                "likely is and estimate its macros as realistically as possible for a typical single "
                "serving shown in the image. Respond with ONLY valid JSON, no other text, in exactly "
                "this shape: "
                '{"title": "short dish name", "description": "1-sentence description", '
                '"calories": number, "protein": number, "carbs": number, "fat": number, '
                '"confidence": "high"|"medium"|"low"}. '
                "If you cannot identify food in the image at all, set title to \"Unrecognized\" and "
                "confidence to \"low\" with all macros at 0."
            ),
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": image_b64}},
                    {"type": "text", "text": f"Estimate the macros for this meal.{context_line}"},
                ],
            }],
        )

        raw_text = response.content[0].text.strip()
        # Claude sometimes wraps JSON in ```json fences despite instructions — strip defensively.
        cleaned = raw_text.replace("```json", "").replace("```", "").strip()
        estimate = json_module.loads(cleaned)

        return {
            "title": estimate.get("title", "Unrecognized meal"),
            "description": estimate.get("description", ""),
            "calories": estimate.get("calories", 0),
            "protein": estimate.get("protein", 0),
            "carbs": estimate.get("carbs", 0),
            "fat": estimate.get("fat", 0),
            "confidence": estimate.get("confidence", "low"),
        }
    except json_module.JSONDecodeError:
        raise HTTPException(status_code=502, detail="Couldn't parse the nutrition estimate — try a clearer photo")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Photo analysis failed: {e}")


# ═══════════════════════════════════════════════════════════════════════
# MACROS
# ═══════════════════════════════════════════════════════════════════════

class MacroLogRequest(BaseModel):
    date: str
    calories: float = 0
    protein: float = 0
    carbs: float = 0
    fat: float = 0

@macros_router.post("/log")
def log_macros(request: MacroLogRequest, db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    user = _resolve_user(db, user_id)
    existing = db.query(MacroLog).filter(MacroLog.user_id == user.id, MacroLog.log_date == request.date).first()

    if existing:
        existing.calories, existing.protein, existing.carbs, existing.fat = request.calories, request.protein, request.carbs, request.fat
        db.commit()
        return {"id": existing.id, "status": "updated"}

    log = MacroLog(id=generate_id("macro"), user_id=user.id, log_date=request.date,
                    calories=request.calories, protein=request.protein, carbs=request.carbs, fat=request.fat)
    db.add(log)
    db.commit()
    db.refresh(log)
    return {"id": log.id, "status": "created"}


@macros_router.get("/{date}")
def get_macros_for_date(date: str, db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    user = _resolve_user(db, user_id)
    log = db.query(MacroLog).filter(MacroLog.user_id == user.id, MacroLog.log_date == date).first()
    if not log:
        return {"calories": 0, "protein": 0, "carbs": 0, "fat": 0}
    return {"id": log.id, "date": log.log_date, "calories": log.calories, "protein": log.protein, "carbs": log.carbs, "fat": log.fat}


@macros_router.get("/weekly/average")
def get_weekly_average(db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    user = _resolve_user(db, user_id)
    today = datetime.utcnow()
    start_date = (today - timedelta(days=7)).strftime("%Y-%m-%d")
    end_date = today.strftime("%Y-%m-%d")

    logs = db.query(MacroLog).filter(MacroLog.user_id == user.id, MacroLog.log_date >= start_date, MacroLog.log_date <= end_date).all()
    if not logs:
        return {"days": [], "averages": {"calories": 0, "protein": 0, "carbs": 0, "fat": 0}}

    n = len(logs)
    return {
        "days": [l.log_date for l in logs],
        "averages": {
            "calories": round(sum(l.calories for l in logs) / n, 1),
            "protein": round(sum(l.protein for l in logs) / n, 1),
            "carbs": round(sum(l.carbs for l in logs) / n, 1),
            "fat": round(sum(l.fat for l in logs) / n, 1),
        },
    }


# ═══════════════════════════════════════════════════════════════════════
# WORKOUTS
# ═══════════════════════════════════════════════════════════════════════

class ExerciseSet(BaseModel):
    reps: int
    weight: float
    notes: Optional[str] = None

class Exercise(BaseModel):
    name: str
    sets: List[ExerciseSet]

class WorkoutLogRequest(BaseModel):
    date: str
    name: str
    duration: int
    exercises: List[Exercise] = []
    energy_level: int = 3
    notes: Optional[str] = None
    completed: bool = False

# ── Exercise library — proxies wger.de's public, no-auth-required API ──────
# wger is an open-source fitness database (~845 exercises, CC-BY-SA 4.0,
# commercial use OK with attribution). We proxy rather than call it
# directly from the frontend so we can cache and normalize the response
# shape, and so API keys/rate limits are never a frontend concern.
import requests as http_requests

WGER_BASE = "https://wger.de/api/v2"
WGER_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; ApexFitnessApp/1.0)"}
_exercise_cache = {"categories": None, "equipment": None}

@workouts_router.get("/exercises/search")
def search_exercises(term: str = "", category: str = None, limit: int = 30):
    """Search the exercise library. Empty term returns a general list."""
    try:
        if term:
            resp = http_requests.get(
                f"{WGER_BASE}/exercise/search/",
                params={"term": term, "language": "english", "format": "json"},
                headers=WGER_HEADERS,
                timeout=8,
            )
            resp.raise_for_status()
            data = resp.json()
            results = [
                {"id": s["data"]["id"], "name": s["data"]["name"], "category": s["data"].get("category")}
                for s in data.get("suggestions", [])[:limit]
            ]
        else:
            params = {"language": 2, "limit": limit, "format": "json"}
            if category:
                params["category"] = category
            resp = http_requests.get(f"{WGER_BASE}/exercise/", params=params, headers=WGER_HEADERS, timeout=8)
            resp.raise_for_status()
            data = resp.json()
            results = [
                {"id": r["id"], "name": r.get("name", "Unnamed"), "category": r.get("category")}
                for r in data.get("results", [])
            ]
        return {"exercises": results}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Exercise library lookup failed: {e}")


@workouts_router.get("/exercises/categories")
def get_exercise_categories():
    """Muscle group / category list — cached in-process since this rarely changes."""
    if _exercise_cache["categories"] is not None:
        return {"categories": _exercise_cache["categories"]}
    try:
        resp = http_requests.get(f"{WGER_BASE}/exercisecategory/", params={"format": "json", "limit": 50}, headers=WGER_HEADERS, timeout=8)
        resp.raise_for_status()
        categories = [{"id": c["id"], "name": c["name"]} for c in resp.json().get("results", [])]
        _exercise_cache["categories"] = categories
        return {"categories": categories}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Category lookup failed: {e}")


@workouts_router.get("/exercises/{exercise_id}")
def get_exercise_detail(exercise_id: int):
    """Full detail for one exercise — description, muscles, equipment, images."""
    try:
        resp = http_requests.get(f"{WGER_BASE}/exerciseinfo/{exercise_id}/", params={"format": "json"}, headers=WGER_HEADERS, timeout=8)
        resp.raise_for_status()
        data = resp.json()

        translations = [t for t in data.get("translations", []) if t.get("language") == 2]
        t = translations[0] if translations else (data.get("translations") or [{}])[0]

        import re, html as html_module
        raw_desc = t.get("description", "")
        clean_desc = re.sub("<[^>]+>", "", html_module.unescape(raw_desc)).strip()

        images = [img.get("image") for img in data.get("images", []) if img.get("image")]

        return {
            "id": data.get("id"),
            "name": t.get("name", "Unnamed"),
            "description": clean_desc,
            "category": (data.get("category") or {}).get("name"),
            "muscles_primary": [m.get("name") for m in data.get("muscles", [])],
            "muscles_secondary": [m.get("name") for m in data.get("muscles_secondary", [])],
            "equipment": [e.get("name") for e in data.get("equipment", [])],
            "images": images,
        }
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Exercise detail lookup failed: {e}")

@workouts_router.post("/log")
def log_workout(request: WorkoutLogRequest, db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    user = _resolve_user(db, user_id)
    workout = WorkoutLog(
        id=generate_id("workout"), user_id=user.id, log_date=request.date, name=request.name,
        duration=request.duration, exercises=[ex.model_dump() for ex in request.exercises],
        energy_level=request.energy_level, notes=request.notes, completed=request.completed,
    )
    db.add(workout)
    db.commit()
    db.refresh(workout)
    return {"id": workout.id, "status": "logged"}


@workouts_router.get("/{date}")
def get_workouts_for_date(date: str, db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    user = _resolve_user(db, user_id)
    workouts = db.query(WorkoutLog).filter(WorkoutLog.user_id == user.id, WorkoutLog.log_date == date).all()
    return {"workouts": [
        {"id": w.id, "name": w.name, "duration": w.duration, "energy_level": w.energy_level,
         "completed": w.completed, "exercises": w.exercises}
        for w in workouts
    ]}


@workouts_router.get("/weekly/summary")
def get_weekly_summary(db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    user = _resolve_user(db, user_id)
    start_date = (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%d")
    workouts = db.query(WorkoutLog).filter(WorkoutLog.user_id == user.id, WorkoutLog.log_date >= start_date, WorkoutLog.completed == True).all()

    total_duration = sum(w.duration for w in workouts) if workouts else 0
    avg_energy = sum(w.energy_level for w in workouts) / len(workouts) if workouts else 0
    return {"completed": len(workouts), "total_minutes": total_duration, "avg_energy": round(avg_energy, 1)}


# ═══════════════════════════════════════════════════════════════════════
# VITALS
# ═══════════════════════════════════════════════════════════════════════

class VitalLogRequest(BaseModel):
    date: str
    water: float = 0
    sleep: float = 0
    energy_level: int = 3
    mood: int = 3

@vitals_router.post("/log")
def log_vitals(request: VitalLogRequest, db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    user = _resolve_user(db, user_id)
    existing = db.query(VitalLog).filter(VitalLog.user_id == user.id, VitalLog.log_date == request.date).first()

    if existing:
        existing.water, existing.sleep, existing.energy_level, existing.mood = request.water, request.sleep, request.energy_level, request.mood
        db.commit()
        return {"id": existing.id, "status": "updated"}

    vital = VitalLog(id=generate_id("vital"), user_id=user.id, log_date=request.date,
                      water=request.water, sleep=request.sleep, energy_level=request.energy_level, mood=request.mood)
    db.add(vital)
    db.commit()
    db.refresh(vital)
    return {"id": vital.id, "status": "created"}


@vitals_router.get("/{date}")
def get_vitals_for_date(date: str, db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    user = _resolve_user(db, user_id)
    vital = db.query(VitalLog).filter(VitalLog.user_id == user.id, VitalLog.log_date == date).first()
    if not vital:
        return {"water": 0, "sleep": 0, "energy_level": 3, "mood": 3}
    return {"date": vital.log_date, "water": vital.water, "sleep": vital.sleep, "energy_level": vital.energy_level, "mood": vital.mood}


# ═══════════════════════════════════════════════════════════════════════
# BODY METRICS
# ═══════════════════════════════════════════════════════════════════════

class BodyMetricsRequest(BaseModel):
    date: str
    weight: Optional[float] = None
    body_fat: Optional[float] = None
    measurements: dict = {}  # e.g. {"waist": 33.5, "chest": 40, "arms": 15.2}

@body_router.post("/log")
def log_body_metrics(request: BodyMetricsRequest, db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    user = _resolve_user(db, user_id)
    existing = db.query(BodyMetrics).filter(BodyMetrics.user_id == user.id, BodyMetrics.metric_date == request.date).first()

    if existing:
        existing.weight = request.weight
        existing.body_fat = request.body_fat
        existing.measurements = request.measurements
        db.commit()
        return {"id": existing.id, "status": "updated"}

    entry = BodyMetrics(
        id=generate_id("body"), user_id=user.id, metric_date=request.date,
        weight=request.weight, body_fat=request.body_fat, measurements=request.measurements,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return {"id": entry.id, "status": "created"}


@body_router.get("/latest")
def get_latest_body_metrics(db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    """
    Most recent entry, plus a real week-over-week delta computed from actual
    logged history — never a hardcoded "-1.2 this week" placeholder.
    """
    user = _resolve_user(db, user_id)
    entries = db.query(BodyMetrics).filter(BodyMetrics.user_id == user.id).order_by(BodyMetrics.metric_date.desc()).limit(30).all()

    if not entries:
        return {"latest": None, "trends": {}}

    latest = entries[0]
    week_ago_cutoff = (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%d")
    older_entries = [e for e in entries if e.metric_date <= week_ago_cutoff]
    baseline = older_entries[0] if older_entries else (entries[-1] if len(entries) > 1 else None)

    def delta(current, base):
        if current is None or base is None:
            return None
        return round(current - base, 1)

    trends = {"weight": None, "body_fat": None, "measurements": {}}
    if baseline:
        trends["weight"] = delta(latest.weight, baseline.weight)
        trends["body_fat"] = delta(latest.body_fat, baseline.body_fat)
        for key in (latest.measurements or {}):
            base_val = (baseline.measurements or {}).get(key)
            trends["measurements"][key] = delta((latest.measurements or {}).get(key), base_val)

    return {
        "latest": {
            "date": latest.metric_date,
            "weight": latest.weight,
            "body_fat": latest.body_fat,
            "measurements": latest.measurements,
        },
        "trends": trends,
    }


# ═══════════════════════════════════════════════════════════════════════
# COMMUNITY — groups, challenges
# ═══════════════════════════════════════════════════════════════════════

class GroupCreateRequest(BaseModel):
    name: str
    description: Optional[str] = None
    privacy: str = "private"
    group_type: str = "general"  # general, challenge, accountability
    settings: dict = {}

class GroupMessageRequest(BaseModel):
    message: str

class ChallengeCreateRequest(BaseModel):
    name: str
    description: Optional[str] = None
    category: str
    end_date: str

@community_router.post("/groups/create")
def create_group(request: GroupCreateRequest, db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    user = _resolve_user(db, user_id)
    group = Group(
        id=generate_id("group"), creator_id=user.id, name=request.name,
        description=request.description, members=[user.id], privacy=request.privacy,
        group_type=request.group_type, settings=request.settings,
    )
    db.add(group)
    db.commit()
    return {
        "id": group.id, "name": group.name, "group_type": group.group_type,
        "members": group.members, "privacy": group.privacy,
    }


@community_router.get("/groups")
def list_my_groups(db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    """Real list of groups the current user belongs to. Empty list if none — never fake data."""
    user = _resolve_user(db, user_id)
    all_groups = db.query(Group).all()
    mine = [g for g in all_groups if user.id in (g.members or [])]
    return {
        "groups": [
            {
                "id": g.id, "name": g.name, "description": g.description,
                "members": len(g.members or []), "group_type": g.group_type,
                "privacy": g.privacy,
            }
            for g in mine
        ]
    }


@community_router.get("/groups/{group_id}")
def get_group(group_id: str, db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    group = db.query(Group).filter(Group.id == group_id).first()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    return {
        "id": group.id, "name": group.name, "description": group.description,
        "members": group.members, "privacy": group.privacy,
        "group_type": group.group_type, "settings": group.settings,
    }


@community_router.post("/groups/{group_id}/message")
def send_group_message(group_id: str, request: GroupMessageRequest, db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    user = _resolve_user(db, user_id)
    group = db.query(Group).filter(Group.id == group_id).first()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    if user.id not in group.members:
        raise HTTPException(status_code=403, detail="Not a group member")

    message = GroupMessage(id=generate_id("msg"), group_id=group_id, sender_id=user.id, message=request.message)
    db.add(message)
    db.commit()
    return {"id": message.id}


@community_router.get("/groups/{group_id}/messages")
def get_group_messages(group_id: str, limit: int = 50, db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    group = db.query(Group).filter(Group.id == group_id).first()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")

    messages = db.query(GroupMessage).filter(GroupMessage.group_id == group_id).order_by(GroupMessage.created_at.desc()).limit(limit).all()
    return {"messages": [{"id": m.id, "sender_id": m.sender_id, "message": m.message, "created_at": m.created_at.isoformat()} for m in messages]}


@community_router.post("/challenges/create")
def create_challenge(request: ChallengeCreateRequest, db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    user = _resolve_user(db, user_id)
    challenge = Challenge(
        id=generate_id("challenge"), name=request.name, description=request.description, creator_id=user.id,
        category=request.category, start_date=datetime.utcnow(),
        end_date=datetime.strptime(request.end_date, "%Y-%m-%d"), participants=[user.id],
    )
    db.add(challenge)
    db.commit()
    return {"id": challenge.id, "name": challenge.name}


@community_router.get("/challenges")
def get_challenges(category: str = None, db: Session = Depends(get_db)):
    query = db.query(Challenge).filter(Challenge.end_date > datetime.utcnow())
    if category:
        query = query.filter(Challenge.category == category)
    challenges = query.all()
    return {"challenges": [
        {"id": c.id, "name": c.name, "category": c.category, "participants": len(c.participants), "end_date": c.end_date.isoformat()}
        for c in challenges
    ]}


@community_router.post("/challenges/{challenge_id}/join")
def join_challenge(challenge_id: str, db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    user = _resolve_user(db, user_id)
    challenge = db.query(Challenge).filter(Challenge.id == challenge_id).first()
    if not challenge:
        raise HTTPException(status_code=404, detail="Challenge not found")
    if user.id not in challenge.participants:
        challenge.participants.append(user.id)
        db.commit()
    return {"status": "joined"}
