"""Language Reader — a self-hosted reader for learning Russian and Finnish.

Environment:
    DATA_DIR     folder for reader.db (default: ./data)
    PORT, HOST   listen address (default: 8080, 0.0.0.0)
    AUTH_TOKEN   if set, every /api request must carry it as a Bearer token,
                 an X-Token header or a `token` query parameter
    LLM_PROVIDER, LLM_MODEL, LLM_BASE_URL, LLM_API_KEY (or ANTHROPIC_API_KEY),
    LLM_EFFORT, PASSAGE_PROVIDER, DEEPL_API_KEY, TARGET_LANGUAGE
                 defaults for the Settings page; values saved there win
"""
from __future__ import annotations

import csv
import hmac
import html
import io
import json
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from . import analysis, db, languages, llm, morph, settings, srs, textproc, tts, usage

AUTH_TOKEN = os.environ.get("AUTH_TOKEN", "").strip()
TEMPLATE = Path(__file__).resolve().parent / "templates" / "index.html"
STATUSES = {"new", "learning", "known"}

@asynccontextmanager
async def _lifespan(_app):
    db.init()
    yield


app = FastAPI(title="Language Reader", docs_url="/api/docs", openapi_url="/api/openapi.json", lifespan=_lifespan)


@app.middleware("http")
async def _auth(request: Request, call_next):
    if AUTH_TOKEN and request.url.path.startswith("/api/"):
        auth = request.headers.get("authorization", "")
        tok = (
            (auth[7:] if auth.lower().startswith("bearer ") else "")
            or request.headers.get("x-token", "")
            or request.query_params.get("token", "")
        )
        if not hmac.compare_digest(tok.encode(), AUTH_TOKEN.encode()):
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    return await call_next(request)


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse(TEMPLATE.read_text(encoding="utf-8"))


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _backend(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except llm.BackendError as e:
        raise HTTPException(502, str(e)) from e


def _settings_for(model: str | None) -> dict:
    """Settings for this request, escalated to another configured model if asked."""
    try:
        return settings.with_model(model)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


def _text_or_404(c, tid: int):
    row = c.execute("SELECT * FROM texts WHERE id = ?", (tid,)).fetchone()
    if not row:
        raise HTTPException(404, "Text not found")
    return row


def _lemma_dict(row) -> dict:
    d = dict(row)
    for k in ("other_meanings", "compound_parts"):
        d[k] = json.loads(d[k]) if d.get(k) else []
    return d


def _surfaces(c, lang: str) -> dict:
    """Lowercased word form -> lemma it was last looked up as, for colouring."""
    out: dict = {}
    for r in c.execute(
        "SELECT ulower(o.surface) AS s, l.id, l.lemma, l.stressed, l.status, l.gloss "
        "FROM occurrences o JOIN lemmas l ON l.id = o.lemma_id WHERE l.lang = ? ORDER BY o.id",
        (lang,),
    ):
        out[r["s"]] = {"lemma_id": r["id"], "lemma": r["stressed"] or r["lemma"], "status": r["status"], "gloss": r["gloss"]}
    return out


# --------------------------------------------------------------------------- #
# texts
# --------------------------------------------------------------------------- #


class NewText(BaseModel):
    lang: str = "auto"
    title: str = ""
    body: str = Field(min_length=1, max_length=200_000)
    source_url: str = ""


class TextPatch(BaseModel):
    title: str | None = None
    source_url: str | None = None


@app.get("/api/texts")
def list_texts(lang: str = "") -> list[dict]:
    sql = """
        SELECT t.id, t.lang, t.title, t.source_url, t.created_at, t.opened_at,
               substr(t.body, 1, 180) AS preview, length(t.body) AS chars,
               (SELECT COUNT(*) FROM sentences s WHERE s.text_id = t.id) AS sentences,
               (SELECT COUNT(*) FROM sentences s WHERE s.text_id = t.id AND s.translation IS NOT NULL) AS translated,
               (SELECT COUNT(*) FROM occurrences o WHERE o.text_id = t.id) AS words,
               (SELECT COUNT(*) FROM phrases p WHERE p.text_id = t.id) AS phrases
        FROM texts t {where}
        ORDER BY COALESCE(t.opened_at, t.created_at) DESC
    """
    with db.tx() as c:
        if lang:
            rows = c.execute(sql.format(where="WHERE t.lang = ?"), (lang,))
        else:
            rows = c.execute(sql.format(where=""))
        return [dict(r) for r in rows]


@app.post("/api/texts")
def create_text(req: NewText) -> dict:
    body = textproc.normalise(req.body.replace("\r\n", "\n")).strip()
    if not body:
        raise HTTPException(400, "The text is empty")
    configured = settings.configured_languages()
    lang = req.lang
    if lang == "auto":
        d = languages.detect(body, configured)
        lang = d["lang"]
        if not lang:
            if d["guess"]:
                raise HTTPException(
                    400,
                    f"This looks like {languages.name(d['guess'])}, which isn't one of your languages. "
                    "Add it in Settings, or choose a language instead of auto-detect.",
                )
            raise HTTPException(400, "Couldn't detect the language. Choose one instead of auto-detect.")
    if lang not in configured:
        raise HTTPException(400, f"{languages.name(lang)} isn't one of your languages. Add it in Settings first.")
    title = req.title.strip() or textproc.default_title(body)
    spans = textproc.split_sentences(body)
    with db.tx() as c:
        # Sending the same text again (a second click in the browser add-on,
        # a re-paste) opens the copy already in the library.
        dup = c.execute("SELECT id FROM texts WHERE lang = ? AND body = ? ORDER BY id LIMIT 1", (lang, body)).fetchone()
        if dup:
            if req.source_url.strip():
                c.execute("UPDATE texts SET source_url = COALESCE(source_url, ?) WHERE id = ?", (req.source_url.strip(), dup["id"]))
            return {"id": dup["id"], "lang": lang, "existing": True}
        cur = c.execute(
            "INSERT INTO texts(lang, title, source_url, body) VALUES(?, ?, ?, ?)",
            (lang, title, req.source_url.strip() or None, body),
        )
        tid = cur.lastrowid
        c.executemany(
            "INSERT INTO sentences(text_id, idx, start, end) VALUES(?, ?, ?, ?)",
            [(tid, i, s, e) for i, (s, e) in enumerate(spans)],
        )
    return {"id": tid, "lang": lang, "existing": False}


class SummaryReq(BaseModel):
    kind: str = "target"
    model: str | None = None


def _summary_dict(r) -> dict:
    return {"kind": r["kind"], "summary": r["summary"], "key_points": json.loads(r["key_points"] or "[]"),
            "model": r["model"], "created_at": r["created_at"]}


@app.post("/api/texts/{tid}/summary")
def summarise_text(tid: int, req: SummaryReq) -> dict:
    if req.kind not in ("target", "simple"):
        raise HTTPException(400, "kind must be target or simple")
    with db.tx() as c:
        t = _text_or_404(c, tid)
    s = _settings_for(req.model)
    a = _backend(analysis.summarise, s, t["lang"], t["title"], t["body"], req.kind)
    with db.tx() as c:
        c.execute(
            """INSERT INTO summaries(text_id, kind, summary, key_points, model) VALUES(?, ?, ?, ?, ?)
               ON CONFLICT(text_id, kind) DO UPDATE SET summary = excluded.summary, key_points = excluded.key_points,
                 model = excluded.model, created_at = datetime('now')""",
            (tid, req.kind, a["summary"].strip(), json.dumps(a["key_points"], ensure_ascii=False), s["llm_model"]),
        )
        r = c.execute("SELECT * FROM summaries WHERE text_id = ? AND kind = ?", (tid, req.kind)).fetchone()
    return _summary_dict(r)


@app.get("/api/texts/{tid}")
def get_text(tid: int) -> dict:
    with db.tx() as c:
        t = dict(_text_or_404(c, tid))
        c.execute("UPDATE texts SET opened_at = datetime('now') WHERE id = ?", (tid,))
        body = t["body"]
        t["sentences"] = [
            {**dict(r), "tokens": textproc.tokens(body, r["start"], r["end"])}
            for r in c.execute(
                "SELECT id, idx, start, end, translation FROM sentences WHERE text_id = ? ORDER BY idx", (tid,)
            )
        ]
        t["occurrences"] = [
            dict(r)
            for r in c.execute(
                "SELECT o.id, o.lemma_id, o.start, o.end, o.surface, o.gloss_in_context, l.status "
                "FROM occurrences o JOIN lemmas l ON l.id = o.lemma_id WHERE o.text_id = ?",
                (tid,),
            )
        ]
        t["phrases"] = [
            dict(r)
            for r in c.execute(
                "SELECT id, start, end, span, translation, literal, notes, kind FROM phrases "
                "WHERE text_id = ? ORDER BY start",
                (tid,),
            )
        ]
        t["surfaces"] = _surfaces(c, t["lang"])
        t["summaries"] = {r["kind"]: _summary_dict(r) for r in c.execute("SELECT * FROM summaries WHERE text_id = ?", (tid,))}
    return t


@app.patch("/api/texts/{tid}")
def patch_text(tid: int, req: TextPatch) -> dict:
    with db.tx() as c:
        _text_or_404(c, tid)
        if req.title is not None and req.title.strip():
            c.execute("UPDATE texts SET title = ? WHERE id = ?", (req.title.strip(), tid))
        if req.source_url is not None:
            c.execute("UPDATE texts SET source_url = ? WHERE id = ?", (req.source_url.strip() or None, tid))
    return {"ok": True}


@app.delete("/api/texts/{tid}")
def delete_text(tid: int) -> dict:
    # Saved words and phrases stay in the vocabulary with their example sentence.
    with db.tx() as c:
        _text_or_404(c, tid)
        c.execute("DELETE FROM texts WHERE id = ?", (tid,))
    return {"ok": True}


class TranslateReq(BaseModel):
    sentence_ids: list[int] | None = None
    force: bool = False
    model: str | None = None  # escalate to another configured model (retranslates)


@app.post("/api/texts/{tid}/translate")
def translate_text(tid: int, req: TranslateReq) -> dict:
    with db.tx() as c:
        t = _text_or_404(c, tid)
        rows = c.execute(
            "SELECT id, start, end, translation FROM sentences WHERE text_id = ? ORDER BY idx", (tid,)
        ).fetchall()
    if req.sentence_ids is not None:
        wanted = set(req.sentence_ids)
        rows = [r for r in rows if r["id"] in wanted]
    if not (req.force or req.model):
        rows = [r for r in rows if r["translation"] is None]
    if rows:
        s = _settings_for(req.model)
        originals = [t["body"][r["start"] : r["end"]] for r in rows]
        out = _backend(analysis.translate_sentences, s, t["lang"], originals)
        with db.tx() as c:
            c.executemany(
                "UPDATE sentences SET translation = ? WHERE id = ?",
                [(tr, r["id"]) for tr, r in zip(out, rows)],
            )
    with db.tx() as c:
        return {
            "translated": len(rows),
            "sentences": [
                dict(r)
                for r in c.execute("SELECT id, translation FROM sentences WHERE text_id = ? ORDER BY idx", (tid,))
            ],
        }


# --------------------------------------------------------------------------- #
# lookups
# --------------------------------------------------------------------------- #


class LookupReq(BaseModel):
    text_id: int
    start: int
    end: int
    refresh: bool = False
    model: str | None = None  # escalate to another configured model


def _sentence_at(c, tid: int, pos: int):
    return c.execute(
        "SELECT id, start, end, translation FROM sentences WHERE text_id = ? AND start <= ? AND end >= ? "
        "ORDER BY idx LIMIT 1",
        (tid, pos, pos),
    ).fetchone()


@app.post("/api/lookup")
def lookup(req: LookupReq) -> dict:
    with db.tx() as c:
        t = _text_or_404(c, req.text_id)
        body = t["body"]
        if not (0 <= req.start < req.end <= len(body)) or req.end - req.start > 80:
            raise HTTPException(400, "Bad word range")
        sent = _sentence_at(c, req.text_id, req.start)
    if not sent:
        raise HTTPException(400, "Word is outside any sentence")
    surface = body[req.start : req.end]
    sentence = body[sent["start"] : sent["end"]]
    s = _settings_for(req.model)
    escalated = bool(req.model)
    a = _backend(
        analysis.analyse_word, s, t["lang"], sentence, req.start - sent["start"], req.end - sent["start"],
        use_cache=not req.refresh,
    )
    lemma = textproc.strip_stress(a["lemma"]).strip() or surface
    pos = a["pos"].strip().lower()
    with db.tx() as c:
        row = c.execute(
            "SELECT * FROM lemmas WHERE lang = ? AND lemma = ? AND pos = ?", (t["lang"], lemma, pos)
        ).fetchone()
        if row and escalated:
            # A stronger model's answer replaces the earlier one, except a
            # CEFR level the user set by hand.
            c.execute(
                """UPDATE lemmas SET stressed = ?, gloss = ?, other_meanings = ?, compound_parts = ?, notes = ?,
                     cefr = CASE WHEN cefr_source = 'manual' THEN cefr ELSE ? END,
                     cefr_source = CASE WHEN cefr_source = 'manual' THEN cefr_source ELSE ? END,
                     updated_at = datetime('now')
                   WHERE id = ?""",
                (
                    a["stressed"] or lemma, a["gloss"], json.dumps(a["other_meanings"], ensure_ascii=False),
                    json.dumps(a["compound_parts"], ensure_ascii=False), a["notes"], a["cefr"] or None,
                    f"LLM estimate ({s['llm_model']})" if a["cefr"] else None, row["id"],
                ),
            )
            lemma_id = row["id"]
        elif row:
            # Keep anything the user has edited; only fill gaps.
            c.execute(
                """UPDATE lemmas SET
                     stressed = COALESCE(NULLIF(stressed, ''), ?),
                     gloss = COALESCE(NULLIF(gloss, ''), ?),
                     other_meanings = COALESCE(other_meanings, ?),
                     compound_parts = COALESCE(compound_parts, ?),
                     cefr = COALESCE(NULLIF(cefr, ''), ?),
                     cefr_source = COALESCE(cefr_source, ?),
                     notes = COALESCE(NULLIF(notes, ''), ?),
                     updated_at = datetime('now')
                   WHERE id = ?""",
                (
                    a["stressed"], a["gloss"], json.dumps(a["other_meanings"], ensure_ascii=False),
                    json.dumps(a["compound_parts"], ensure_ascii=False), a["cefr"] or None,
                    "LLM estimate" if a["cefr"] else None, a["notes"], row["id"],
                ),
            )
            lemma_id = row["id"]
        else:
            lemma_id = c.execute(
                """INSERT INTO lemmas(lang, lemma, pos, stressed, gloss, other_meanings, compound_parts,
                                      cefr, cefr_source, notes)
                   VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    t["lang"], lemma, pos, a["stressed"] or lemma, a["gloss"],
                    json.dumps(a["other_meanings"], ensure_ascii=False),
                    json.dumps(a["compound_parts"], ensure_ascii=False),
                    a["cefr"] or None, "LLM estimate" if a["cefr"] else None, a["notes"],
                ),
            ).lastrowid
        c.execute(
            """INSERT INTO occurrences(lemma_id, text_id, sentence_id, surface, morphology, gloss_in_context,
                                       sentence_text, start, end)
               VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(text_id, start, end) DO UPDATE SET
                 lemma_id = excluded.lemma_id, morphology = excluded.morphology,
                 gloss_in_context = excluded.gloss_in_context""",
            (lemma_id, req.text_id, sent["id"], surface, a["morphology"], a["gloss"], sentence, req.start, req.end),
        )
        # A re-analysis can move an occurrence to a different lemma; drop the
        # old lemma if that left it with no examples and untouched status.
        c.execute(
            "DELETE FROM lemmas WHERE lang = ? AND status = 'new' AND id <> ? "
            "AND NOT EXISTS (SELECT 1 FROM occurrences o WHERE o.lemma_id = lemmas.id)",
            (t["lang"], lemma_id),
        )
        lrow = c.execute("SELECT * FROM lemmas WHERE id = ?", (lemma_id,)).fetchone()
    return {
        "analysis": a,
        "model": s["llm_model"],
        "surface": surface,
        "lemma": _lemma_dict(lrow),
        "sentence": {"id": sent["id"], "text": sentence, "translation": sent["translation"]},
    }


class PhraseReq(BaseModel):
    text_id: int
    start: int
    end: int
    model: str | None = None


@app.post("/api/phrases")
def add_phrase(req: PhraseReq) -> dict:
    with db.tx() as c:
        t = _text_or_404(c, req.text_id)
        body = t["body"]
        if not (0 <= req.start < req.end <= len(body)) or req.end - req.start > 600:
            raise HTTPException(400, "Select up to about 600 characters")
        ctx = c.execute(
            "SELECT start, end FROM sentences WHERE text_id = ? AND end >= ? AND start <= ? ORDER BY idx",
            (req.text_id, req.start, req.end),
        ).fetchall()
    span = body[req.start : req.end]
    context = body[ctx[0]["start"] : ctx[-1]["end"]] if ctx else span
    s = _settings_for(req.model)
    r = _backend(analysis.translate_phrase, s, t["lang"], span, context)
    with db.tx() as c:
        c.execute(
            """INSERT INTO phrases(lang, text_id, start, end, span, context, translation, literal, notes)
               VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(text_id, start, end) DO UPDATE SET
                 translation = excluded.translation, literal = excluded.literal, notes = excluded.notes""",
            (t["lang"], req.text_id, req.start, req.end, span, context, r["translation"], r["literal"], r["notes"]),
        )
        row = c.execute(
            "SELECT id, start, end, span, translation, literal, notes, kind FROM phrases "
            "WHERE text_id = ? AND start = ? AND end = ?",
            (req.text_id, req.start, req.end),
        ).fetchone()
    return {**dict(row), "model": s["llm_model"]}


@app.post("/api/texts/{tid}/sentences/{sid}/phrase")
def save_sentence(tid: int, sid: int) -> dict:
    """Save a sentence and its translation to the phrase book, translating it first if needed."""
    with db.tx() as c:
        t = _text_or_404(c, tid)
        sent = c.execute("SELECT * FROM sentences WHERE id = ? AND text_id = ?", (sid, tid)).fetchone()
    if not sent:
        raise HTTPException(404, "Sentence not found")
    original = t["body"][sent["start"] : sent["end"]]
    translation = sent["translation"]
    if translation is None:
        translation = _backend(analysis.translate_sentences, settings.load(), t["lang"], [original])[0]
        with db.tx() as c:
            c.execute("UPDATE sentences SET translation = ? WHERE id = ?", (translation, sid))
    with db.tx() as c:
        c.execute(
            """INSERT INTO phrases(lang, text_id, start, end, span, context, translation, literal, notes, kind)
               VALUES(?, ?, ?, ?, ?, ?, ?, '', '', 'sentence')
               ON CONFLICT(text_id, start, end) DO NOTHING""",
            (t["lang"], tid, sent["start"], sent["end"], original, original, translation),
        )
        row = c.execute(
            "SELECT id, start, end, span, translation, literal, notes, kind FROM phrases "
            "WHERE text_id = ? AND start = ? AND end = ?",
            (tid, sent["start"], sent["end"]),
        ).fetchone()
    return {**dict(row), "sentence_id": sid, "sentence_translation": translation}


@app.get("/api/phrases")
def list_phrases(lang: str = "", q: str = "") -> list[dict]:
    where, args = [], []
    if lang:
        where.append("p.lang = ?")
        args.append(lang)
    if q:
        where.append("(ulower(p.span) LIKE ? OR ulower(p.translation) LIKE ?)")
        args += [f"%{q.lower()}%"] * 2
    sql = (
        "SELECT p.*, t.title AS text_title FROM phrases p LEFT JOIN texts t ON t.id = p.text_id "
        + ("WHERE " + " AND ".join(where) if where else "")
        + " ORDER BY p.id DESC"
    )
    with db.tx() as c:
        return [dict(r) for r in c.execute(sql, args)]


@app.delete("/api/phrases/{pid}")
def delete_phrase(pid: int) -> dict:
    with db.tx() as c:
        c.execute("DELETE FROM phrases WHERE id = ?", (pid,))
        c.execute("DELETE FROM chats WHERE kind = 'phrase' AND ref_id = ?", (pid,))
    return {"ok": True}


# --------------------------------------------------------------------------- #
# vocabulary
# --------------------------------------------------------------------------- #

_VOCAB_SQL = """
    SELECT l.*, COUNT(o.id) AS occurrences, MAX(o.created_at) AS last_seen,
           (SELECT o2.sentence_text FROM occurrences o2 WHERE o2.lemma_id = l.id ORDER BY o2.id LIMIT 1) AS example,
           (SELECT o2.surface FROM occurrences o2 WHERE o2.lemma_id = l.id ORDER BY o2.id LIMIT 1) AS example_surface,
           (SELECT o2.gloss_in_context FROM occurrences o2 WHERE o2.lemma_id = l.id ORDER BY o2.id LIMIT 1) AS example_gloss,
           (SELECT s.translation FROM occurrences o2 JOIN sentences s ON s.id = o2.sentence_id
              WHERE o2.lemma_id = l.id ORDER BY o2.id LIMIT 1) AS example_translation
    FROM lemmas l LEFT JOIN occurrences o ON o.lemma_id = l.id
    {where}
    GROUP BY l.id
    ORDER BY COALESCE(MAX(o.created_at), l.updated_at) DESC
"""


def _vocab_rows(lang: str, status: str, cefr: str, q: str, starred: bool = False) -> list[dict]:
    where, args = [], []
    if starred:
        where.append("l.starred = 1")
    if lang:
        where.append("l.lang = ?")
        args.append(lang)
    if status:
        where.append("l.status = ?")
        args.append(status)
    if cefr:
        if cefr == "none":
            where.append("l.cefr IS NULL")
        else:
            where.append("l.cefr = ?")
            args.append(cefr)
    if q:
        q = textproc.strip_stress(q.lower())
        where.append(
            "(ulower(l.lemma) LIKE ? OR ulower(l.gloss) LIKE ? OR EXISTS "
            "(SELECT 1 FROM occurrences o3 WHERE o3.lemma_id = l.id AND ulower(o3.surface) LIKE ?))"
        )
        args += [f"%{q}%"] * 3
    sql = _VOCAB_SQL.format(where=("WHERE " + " AND ".join(where)) if where else "")
    with db.tx() as c:
        return [_lemma_dict(r) for r in c.execute(sql, args)]


@app.get("/api/vocab")
def list_vocab(lang: str = "", status: str = "", cefr: str = "", q: str = "", starred: bool = False) -> list[dict]:
    return _vocab_rows(lang, status, cefr, q, starred)


@app.get("/api/vocab/{lid}")
def get_vocab(lid: int) -> dict:
    with db.tx() as c:
        row = c.execute("SELECT * FROM lemmas WHERE id = ?", (lid,)).fetchone()
        if not row:
            raise HTTPException(404, "Word not found")
        d = _lemma_dict(row)
        d["occurrences"] = [
            dict(r)
            for r in c.execute(
                """SELECT o.id, o.text_id, o.surface, o.morphology, o.gloss_in_context, o.sentence_text,
                          o.start, o.created_at, t.title AS text_title, s.start AS sentence_start,
                          s.translation AS sentence_translation
                   FROM occurrences o LEFT JOIN texts t ON t.id = o.text_id
                   LEFT JOIN sentences s ON s.id = o.sentence_id
                   WHERE o.lemma_id = ? ORDER BY o.id DESC""",
                (lid,),
            )
        ]
    return d


class VocabPatch(BaseModel):
    status: str | None = None
    gloss: str | None = None
    notes: str | None = None
    cefr: str | None = None
    starred: bool | None = None


@app.patch("/api/vocab/{lid}")
def patch_vocab(lid: int, req: VocabPatch) -> dict:
    sets, args = [], []
    if req.status is not None:
        if req.status not in STATUSES:
            raise HTTPException(400, "status must be new, learning or known")
        sets.append("status = ?")
        args.append(req.status)
    if req.gloss is not None:
        sets.append("gloss = ?")
        args.append(req.gloss.strip())
    if req.notes is not None:
        sets.append("notes = ?")
        args.append(req.notes.strip())
    if req.starred is not None:
        sets.append("starred = ?")
        args.append(int(req.starred))
    if req.cefr is not None:
        if req.cefr and req.cefr not in analysis.CEFR:
            raise HTTPException(400, "cefr must be A1–C2 or blank")
        sets += ["cefr = ?", "cefr_source = ?"]
        args += [req.cefr or None, "manual" if req.cefr else None]
    if not sets:
        raise HTTPException(400, "Nothing to update")
    with db.tx() as c:
        c.execute(f"UPDATE lemmas SET {', '.join(sets)}, updated_at = datetime('now') WHERE id = ?", (*args, lid))
        row = c.execute("SELECT * FROM lemmas WHERE id = ?", (lid,)).fetchone()
    if not row:
        raise HTTPException(404, "Word not found")
    return _lemma_dict(row)


@app.delete("/api/vocab/{lid}")
def delete_vocab(lid: int) -> dict:
    with db.tx() as c:
        c.execute("DELETE FROM lemmas WHERE id = ?", (lid,))
        c.execute("DELETE FROM chats WHERE kind = 'word' AND ref_id = ?", (lid,))
    return {"ok": True}


@app.get("/api/export.csv")
def export_csv(lang: str = "", status: str = "", cefr: str = "", starred: bool = False) -> Response:
    rows = _vocab_rows(lang, status, cefr, "", starred)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["lang", "lemma", "stressed", "pos", "gloss", "other_meanings", "cefr", "status",
                "example", "example_translation", "notes", "starred"])
    for r in rows:
        w.writerow([r["lang"], r["lemma"], r["stressed"], r["pos"], r["gloss"], "; ".join(r["other_meanings"]),
                    r["cefr"] or "", r["status"], r["example"] or "", r["example_translation"] or "", r["notes"] or "",
                    "yes" if r.get("starred") else ""])
    name = f"vocabulary-{lang or 'all'}-{time.strftime('%Y%m%d')}.csv"
    return Response(
        "﻿" + buf.getvalue(),  # BOM so spreadsheet apps read UTF-8
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


# --------------------------------------------------------------------------- #
# Anki export
# --------------------------------------------------------------------------- #


def _anki_field(x: str | None) -> str:
    return html.escape(x or "", quote=False).replace("\t", " ").replace("\r", "").replace("\n", "<br>")


def _anki_mark(sentence: str, surface: str, fmt_word) -> str | None:
    """Sentence with the first occurrence of surface replaced by fmt_word(escaped word)."""
    i = sentence.find(surface) if surface else -1
    if i < 0:
        i = sentence.lower().find(surface.lower()) if surface else -1
    if i < 0:
        return None
    word = sentence[i : i + len(surface)]
    return _anki_field(sentence[:i]) + fmt_word(_anki_field(word)) + _anki_field(sentence[i + len(surface) :])


def _anki_tag(x: str | None) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_:" else "_" for ch in (x or "")).strip("_")


@app.get("/api/export/anki.txt")
def export_anki(lang: str = "", status: str = "", cefr: str = "", starred: bool = False,
                what: str = "words", notetype: str = "basic") -> Response:
    """Tab-separated notes for Anki's File > Import (Anki 2.1.55+ reads the
    header lines). A GUID column makes a re-import update the same notes."""
    if what not in ("words", "phrases") or notetype not in ("basic", "cloze"):
        raise HTTPException(400, "what must be words or phrases; notetype basic or cloze")
    cloze = notetype == "cloze"
    lines = []
    if what == "words":
        for r in _vocab_rows(lang, status, cefr, "", starred):
            head = _anki_field(r["stressed"] or r["lemma"])
            meaning = _anki_field(r["gloss"])
            tr = f"<br><i>{_anki_field(r['example_translation'])}</i>" if r.get("example_translation") else ""
            sentence, surface = r.get("example") or "", r.get("example_surface") or ""
            if cloze:
                # Hint with the meaning in this sentence ("in the library"), which fits the gap
                # better than the dictionary meaning ("library").
                ctx = _anki_field(r.get("example_gloss")) if _anki_mark(sentence, surface, str) else ""
                hint_text = (ctx or meaning).replace("}", ")")
                hint = "::" + hint_text if hint_text else ""
                text = _anki_mark(sentence, surface, lambda w: "{{c1::" + w + hint + "}}") or ("{{c1::" + head + hint + "}}")
                back = f"<b>{head}</b> — {meaning}{tr}"
            else:
                text = _anki_mark(sentence, surface, lambda w: f"<b>{w}</b>") or head
                pos = f" <i>({_anki_field(r['pos'])})</i>" if r.get("pos") else ""
                back = f"<b>{head}</b>{pos} — {meaning}{tr}"
            tags = " ".join(t for t in (r["lang"], _anki_tag(r["cefr"]), r["status"], "starred" if r.get("starred") else "") if t)
            lines.append([f"lr-{notetype}-w{r['id']}", text, back, tags])
    else:
        where, args = [], []
        if lang:
            where.append("lang = ?")
            args.append(lang)
        with db.tx() as c:
            rows = [dict(x) for x in c.execute(
                "SELECT * FROM phrases" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id", args)]
        for r in rows:
            tr = _anki_field(r["translation"])
            lit = f"<br><small>Literally: {_anki_field(r['literal'])}</small>" if r.get("literal") and r["literal"] != r["translation"] else ""
            if cloze and r["kind"] != "sentence":
                hint = "::" + tr.replace("}", ")") if tr else ""
                text = _anki_mark(r.get("context") or "", r["span"], lambda w: "{{c1::" + w + hint + "}}") \
                    or ("{{c1::" + _anki_field(r["span"]) + hint + "}}")
                back = tr + lit
            elif cloze:
                text = "{{c1::" + _anki_field(r["span"]) + "}}"
                back = tr + lit
            else:
                text = _anki_field(r["span"])
                back = tr + lit
            lines.append([f"lr-{notetype}-p{r['id']}", text, back, " ".join(t for t in (r["lang"], "phrase") if t)])
    deck = "Language Reader" + (f"::{languages.name(lang)}" if lang else "")
    header = [
        "#separator:tab", "#html:true", f"#notetype:{'Cloze' if cloze else 'Basic'}",
        f"#deck:{deck}", "#guid column:1", "#tags column:4",
    ]
    body = "\n".join(header + ["\t".join(l) for l in lines]) + "\n"
    name = f"anki-{what}-{notetype}-{lang or 'all'}-{time.strftime('%Y%m%d')}.txt"
    return Response(body, media_type="text/plain; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


# --------------------------------------------------------------------------- #
# flashcard review
# --------------------------------------------------------------------------- #

_CEFR_RANK = "CASE l.cefr WHEN 'A1' THEN 1 WHEN 'A2' THEN 2 WHEN 'B1' THEN 3 WHEN 'B2' THEN 4 WHEN 'C1' THEN 5 WHEN 'C2' THEN 6 ELSE 7 END"


def _card(c, row, at) -> dict:
    """A flashcard: the word with one of its example sentences, split around it."""
    state = None
    if row["last_review"] is not None:
        state = {k: row[k] for k in ("due", "stability", "difficulty", "reps", "lapses", "last_review")}
    occ = c.execute(
        """SELECT o.surface, o.sentence_text, o.gloss_in_context, o.start, s.start AS sstart, s.translation
           FROM occurrences o LEFT JOIN sentences s ON s.id = o.sentence_id
           WHERE o.lemma_id = ? AND o.sentence_text IS NOT NULL AND o.sentence_text <> ''
           ORDER BY o.id DESC""",
        (row["id"],),
    ).fetchall()
    example = None
    if occ:
        o = occ[(state["reps"] if state else 0) % len(occ)]  # a different sentence each review
        sent, surf = o["sentence_text"], o["surface"]
        i = o["start"] - o["sstart"] if o["sstart"] is not None else -1
        if not (0 <= i and sent[i : i + len(surf)] == surf):
            i = sent.find(surf)
        example = {
            "before": sent[:i] if i >= 0 else sent,
            "word": surf if i >= 0 else "",
            "after": sent[i + len(surf) :] if i >= 0 else "",
            "translation": o["translation"],
            "gloss_in_context": o["gloss_in_context"],
        }
    d = _lemma_dict({k: row[k] for k in row.keys() if k not in ("due", "stability", "difficulty", "reps", "lapses", "last_review", "n")})
    d["example"] = example
    d["new"] = state is None
    d["reps"] = state["reps"] if state else 0
    d["labels"] = {str(g): srs.label(state, g, at) for g in (1, 2, 3, 4)}
    return d


def _review_where(lang: str, starred: bool) -> tuple[str, list]:
    where, args = ["l.status <> 'known'"], []
    if lang:
        where.append("l.lang = ?")
        args.append(lang)
    if starred:
        where.append("l.starred = 1")
    return " AND ".join(where), args


@app.get("/api/review")
def review_queue(lang: str = "", starred: bool = False, new_limit: int = 20, count_only: bool = False) -> dict:
    at = srs.now()
    new_limit = min(max(new_limit, 0), 500)
    w, args = _review_where(lang, starred)
    cols = "l.*, r.due, r.stability, r.difficulty, r.reps, r.lapses, r.last_review"
    with db.tx() as c:
        due_rows = c.execute(
            f"SELECT {cols} FROM lemmas l JOIN review_state r ON r.lemma_id = l.id "
            f"WHERE {w} AND r.due <= ? ORDER BY r.due LIMIT 200",
            (*args, srs.fmt(at)),
        ).fetchall()
        due = c.execute(
            f"SELECT COUNT(*) FROM lemmas l JOIN review_state r ON r.lemma_id = l.id WHERE {w} AND r.due <= ?",
            (*args, srs.fmt(at)),
        ).fetchone()[0]
        log_lang = (" AND lang = ?", [lang]) if lang else ("", [])
        new_today = c.execute(
            "SELECT COUNT(*) FROM review_log WHERE was_new = 1 AND date(reviewed_at) = date('now')" + log_lang[0],
            log_lang[1],
        ).fetchone()[0]
        reviewed_today = c.execute(
            "SELECT COUNT(*) FROM review_log WHERE date(reviewed_at) = date('now')" + log_lang[0], log_lang[1]
        ).fetchone()[0]
        new_left = max(0, new_limit - new_today)
        unseen = f"{w} AND NOT EXISTS (SELECT 1 FROM review_state r WHERE r.lemma_id = l.id)"
        new_total = c.execute(f"SELECT COUNT(*) FROM lemmas l WHERE {unseen}", args).fetchone()[0]
        if count_only:
            return {"due": due, "new_total": new_total, "new_today": new_today, "new_left": new_left,
                    "reviewed_today": reviewed_today}
        # Most useful first: lower CEFR level, then words met most often.
        new_rows = c.execute(
            f"""SELECT {cols} FROM lemmas l LEFT JOIN review_state r ON r.lemma_id = l.id
                WHERE {unseen}
                ORDER BY {_CEFR_RANK}, (SELECT COUNT(*) FROM occurrences o WHERE o.lemma_id = l.id) DESC, l.id
                LIMIT ?""",
            (*args, new_left),
        ).fetchall()
        later = c.execute(
            f"SELECT MIN(r.due) FROM lemmas l JOIN review_state r ON r.lemma_id = l.id WHERE {w} AND r.due > ?",
            (*args, srs.fmt(at)),
        ).fetchone()[0]
        cards = [_card(c, r, at) for r in [*due_rows, *new_rows]]
    return {
        "cards": cards, "due": due, "new_total": new_total, "new_today": new_today, "new_left": new_left,
        "reviewed_today": reviewed_today, "next_due": later,
    }


class ReviewReq(BaseModel):
    rating: int = Field(ge=1, le=4)


@app.post("/api/review/{lid}")
def review_card(lid: int, req: ReviewReq) -> dict:
    at = srs.now()
    with db.tx() as c:
        lemma = c.execute("SELECT * FROM lemmas WHERE id = ?", (lid,)).fetchone()
        if not lemma:
            raise HTTPException(404, "Word not found")
        st = c.execute("SELECT * FROM review_state WHERE lemma_id = ?", (lid,)).fetchone()
        nxt = srs.schedule(dict(st) if st else None, req.rating, at)
        c.execute(
            """INSERT INTO review_state(lemma_id, due, stability, difficulty, reps, lapses, last_review)
               VALUES(?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(lemma_id) DO UPDATE SET due = excluded.due, stability = excluded.stability,
                 difficulty = excluded.difficulty, reps = excluded.reps, lapses = excluded.lapses,
                 last_review = excluded.last_review""",
            (lid, nxt["due"], nxt["stability"], nxt["difficulty"], nxt["reps"], nxt["lapses"], nxt["last_review"]),
        )
        c.execute(
            "INSERT INTO review_log(lemma_id, lang, rating, was_new, interval_days) VALUES(?, ?, ?, ?, ?)",
            (lid, lemma["lang"], req.rating, int(st is None), nxt["interval_days"]),
        )
        if lemma["status"] == "new":
            c.execute("UPDATE lemmas SET status = 'learning', updated_at = datetime('now') WHERE id = ?", (lid,))
        card = None
        if req.rating == 1:  # comes back in this session
            row = c.execute(
                "SELECT l.*, r.due, r.stability, r.difficulty, r.reps, r.lapses, r.last_review "
                "FROM lemmas l JOIN review_state r ON r.lemma_id = l.id WHERE l.id = ?",
                (lid,),
            ).fetchone()
            card = _card(c, row, srs.parse(nxt["due"]))
    return {"due": nxt["due"], "interval_days": nxt["interval_days"], "card": card}


@app.get("/api/stats")
def stats() -> dict:
    with db.tx() as c:
        out: dict = {}
        for r in c.execute(
            "SELECT lang, COALESCE(cefr, '?') AS cefr, status, COUNT(*) AS n FROM lemmas GROUP BY 1, 2, 3"
        ):
            lang = out.setdefault(r["lang"], {"total": 0, "by_cefr": {}, "by_status": {}})
            lang["total"] += r["n"]
            lang["by_cefr"][r["cefr"]] = lang["by_cefr"].get(r["cefr"], 0) + r["n"]
            lang["by_status"][r["status"]] = lang["by_status"].get(r["status"], 0) + r["n"]
        for r in c.execute("SELECT lang, COUNT(*) AS n FROM texts GROUP BY lang"):
            out.setdefault(r["lang"], {"total": 0, "by_cefr": {}, "by_status": {}})["texts"] = r["n"]
        for r in c.execute("SELECT lang, COUNT(*) AS n FROM phrases GROUP BY lang"):
            out.setdefault(r["lang"], {"total": 0, "by_cefr": {}, "by_status": {}})["phrases"] = r["n"]
    return out


class DetectReq(BaseModel):
    text: str = Field(max_length=200_000)


@app.post("/api/detect")
def detect(req: DetectReq) -> dict:
    d = languages.detect(req.text, settings.configured_languages())
    return {
        **d,
        "name": languages.name(d["lang"]) if d["lang"] else None,
        "guess_name": languages.name(d["guess"]) if d["guess"] else None,
    }


def _languages_payload() -> dict:
    return {
        "configured": languages.describe(settings.configured_languages()),
        "available": languages.available(),
    }


@app.get("/api/languages")
def get_languages() -> dict:
    return _languages_payload()


@app.get("/api/models")
def get_models() -> dict:
    s = settings.load()
    return {"provider": s["llm_provider"], "default": s["llm_model"], "models": settings.models(s),
            "target_language": s["target_language"]}


@app.get("/api/usage")
def get_usage() -> dict:
    s = settings.load()
    table = usage.prices(s["llm_prices"])
    return {
        **usage.summary(),
        "prices": {m: table.get(m) for m in settings.models(s)},
    }


@app.delete("/api/usage")
def reset_usage() -> dict:
    usage.reset()
    return {"ok": True}


# --------------------------------------------------------------------------- #
# chat about a word or phrase
# --------------------------------------------------------------------------- #

CHAT_SYSTEM = """You are a friendly, precise {lang} tutor for an adult learner whose working language is {target}. \
The learner is reading a {lang} text and has follow-up questions about one {kind}, described below. \
Answer in {target}. Be concise: a few sentences or a short list, unless asked for more. \
Give {lang} examples where they help{stress}. Use simple Markdown only (bold, italics, bullet lists).

What the learner is looking at:
{context}"""
CHAT_HISTORY = 40  # messages sent back to the model; older ones stay saved but aren't resent


def _chat_context(c, kind: str, ref_id: int, text_id: int | None) -> tuple[str, str]:
    """(lang, description) of the word or phrase a chat is about."""
    if kind == "word":
        L = c.execute("SELECT * FROM lemmas WHERE id = ?", (ref_id,)).fetchone()
        if not L:
            raise HTTPException(404, "Word not found")
        occ = None
        if text_id is not None:
            occ = c.execute(
                "SELECT o.*, s.translation FROM occurrences o LEFT JOIN sentences s ON s.id = o.sentence_id "
                "WHERE o.lemma_id = ? AND o.text_id = ? ORDER BY o.id DESC LIMIT 1", (ref_id, text_id)).fetchone()
        if occ is None:
            occ = c.execute(
                "SELECT o.*, s.translation FROM occurrences o LEFT JOIN sentences s ON s.id = o.sentence_id "
                "WHERE o.lemma_id = ? ORDER BY o.id DESC LIMIT 1", (ref_id,)).fetchone()
        lines = [f"Word (dictionary form): {L['stressed'] or L['lemma']} ({L['pos']})", f"Meaning: {L['gloss'] or '?'}"]
        if L["other_meanings"] and json.loads(L["other_meanings"]):
            lines.append("Other meanings: " + "; ".join(json.loads(L["other_meanings"])))
        if L["notes"]:
            lines.append(f"Notes: {L['notes']}")
        if L["cefr"]:
            lines.append(f"CEFR level: {L['cefr']}")
        if occ:
            lines.append(f"Form in the text: {occ['surface']}" + (f" ({occ['morphology']})" if occ["morphology"] else ""))
            lines.append(f"Sentence: {occ['sentence_text']}")
            if occ["translation"]:
                lines.append(f"Sentence translation: {occ['translation']}")
        return L["lang"], "\n".join(lines)
    if kind == "phrase":
        P = c.execute("SELECT * FROM phrases WHERE id = ?", (ref_id,)).fetchone()
        if not P:
            raise HTTPException(404, "Phrase not found")
        lines = [f"Phrase: {P['span']}", f"Translation: {P['translation'] or '?'}"]
        if P["literal"]:
            lines.append(f"Literal translation: {P['literal']}")
        if P["notes"]:
            lines.append(f"Notes: {P['notes']}")
        if P["context"] and P["context"] != P["span"]:
            lines.append(f"Context: {P['context']}")
        return P["lang"], "\n".join(lines)
    raise HTTPException(400, "kind must be word or phrase")


def _chat_messages(c, chat_id: int) -> list[dict]:
    return [dict(r) for r in c.execute(
        "SELECT id, role, content, model, created_at FROM chat_messages WHERE chat_id = ? ORDER BY id", (chat_id,))]


@app.get("/api/chat")
def get_chat(kind: str, ref_id: int) -> dict:
    with db.tx() as c:
        chat = c.execute("SELECT * FROM chats WHERE kind = ? AND ref_id = ?", (kind, ref_id)).fetchone()
        return {"chat": dict(chat) if chat else None, "messages": _chat_messages(c, chat["id"]) if chat else []}


class ChatReq(BaseModel):
    kind: str
    ref_id: int
    message: str = Field(min_length=1, max_length=4000)
    text_id: int | None = None  # for a word: use its sentence in this text as context
    model: str | None = None


@app.post("/api/chat")
def post_chat(req: ChatReq) -> dict:
    s = _settings_for(req.model)
    with db.tx() as c:
        lang, context = _chat_context(c, req.kind, req.ref_id, req.text_id)
        chat = c.execute("SELECT id FROM chats WHERE kind = ? AND ref_id = ?", (req.kind, req.ref_id)).fetchone()
        history = _chat_messages(c, chat["id"]) if chat else []
    name = languages.name(lang)
    system = CHAT_SYSTEM.format(
        lang=name, target=s["target_language"], kind=req.kind, context=context,
        stress=" (mark the stressed vowel in Russian examples with an acute accent)" if lang == "ru" else "",
    )
    msgs = [{"role": m["role"], "content": m["content"]} for m in history[-CHAT_HISTORY:]]
    if msgs and msgs[0]["role"] != "user":
        msgs = msgs[1:]
    msgs.append({"role": "user", "content": req.message.strip()})
    reply, tokens = _backend(llm.chat, s, system, msgs)
    # Saved only once the answer arrives, so a failed call leaves nothing half-done.
    with db.tx() as c:
        c.execute("INSERT OR IGNORE INTO chats(kind, ref_id, lang) VALUES(?, ?, ?)", (req.kind, req.ref_id, lang))
        chat_id = c.execute("SELECT id FROM chats WHERE kind = ? AND ref_id = ?", (req.kind, req.ref_id)).fetchone()["id"]
        c.execute("INSERT INTO chat_messages(chat_id, role, content) VALUES(?, 'user', ?)", (chat_id, req.message.strip()))
        c.execute(
            "INSERT INTO chat_messages(chat_id, role, content, model, input_tokens, output_tokens) VALUES(?, 'assistant', ?, ?, ?, ?)",
            (chat_id, reply, s["llm_model"], tokens[0], tokens[1]),
        )
        return {"chat_id": chat_id, "messages": _chat_messages(c, chat_id)}


@app.delete("/api/chat/{chat_id}")
def delete_chat(chat_id: int) -> dict:
    with db.tx() as c:
        c.execute("DELETE FROM chats WHERE id = ?", (chat_id,))
    return {"ok": True}


# --------------------------------------------------------------------------- #
# speech (Piper)
# --------------------------------------------------------------------------- #


def _tts(fn, *args):
    try:
        return fn(*args)
    except tts.TTSError as e:
        raise HTTPException(400, str(e)) from e


@app.get("/api/tts/config")
def tts_config() -> dict:
    s = settings.load()
    selected = settings.tts_voices(s)
    have = set(tts.installed())
    return {
        "engine": s["tts_engine"] if tts.available() else "browser",
        "piper_available": tts.available(),
        "voices": {lang: key for lang, key in selected.items() if key in have},
    }


@app.get("/api/tts/voices")
def tts_voices() -> dict:
    """Piper voices for each configured language, with download progress."""
    if not tts.available():
        return {"piper_available": False}
    s = settings.load()
    try:
        tts.catalogue()
        catalogue_error = None
    except tts.TTSError as e:
        catalogue_error = str(e)
    return {
        "piper_available": True,
        "catalogue_error": catalogue_error,
        "languages": {lang: tts.voices_for(lang) for lang in settings.configured_languages()},
        "selected": settings.tts_voices(s),
        "downloads": tts.download_status(),
        "cache_bytes": tts.cache_size(),
    }


@app.post("/api/tts/voices/{key}")
def tts_download(key: str) -> dict:
    _tts(tts.start_download, key)
    return {"downloads": tts.download_status()}


@app.delete("/api/tts/voices/{key}")
def tts_remove(key: str) -> dict:
    _tts(tts.remove, key)
    s = settings.load()
    sel = {lang: k for lang, k in settings.tts_voices(s).items() if k != key}
    settings.save({"tts_voices": json.dumps(sel)})
    return {"ok": True}


@app.delete("/api/tts/cache")
def tts_clear_cache() -> dict:
    tts.clear_cache()
    return {"ok": True}


class SpeakReq(BaseModel):
    text: str = Field(min_length=1, max_length=tts.MAX_TEXT * 2)
    lang: str
    speed: float = 1.0
    voice: str | None = None  # defaults to the voice chosen for the language


@app.post("/api/tts/speak")
def tts_speak(req: SpeakReq) -> Response:
    if not tts.available():
        raise HTTPException(400, "Piper isn't installed on the server")
    key = req.voice or settings.tts_voices().get(req.lang)
    if not key:
        raise HTTPException(404, f"No Piper voice chosen for {languages.name(req.lang)}. Pick one in Settings → Speech.")
    audio = _tts(tts.synthesize, textproc.strip_stress(req.text), key, req.speed)
    return Response(audio, media_type="audio/wav", headers={"Cache-Control": "private, max-age=86400"})


# --------------------------------------------------------------------------- #
# settings
# --------------------------------------------------------------------------- #


class SettingsReq(BaseModel):
    values: dict[str, str | None] = {}
    clear: list[str] = []


@app.get("/api/settings")
def get_settings() -> dict:
    return {"settings": settings.public(), "analysers": morph.status(), "languages": _languages_payload()}


@app.put("/api/settings")
def put_settings(req: SettingsReq) -> dict:
    try:
        settings.save(req.values, req.clear)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"settings": settings.public(), "analysers": morph.status(), "languages": _languages_payload()}


@app.post("/api/settings/test")
def test_settings() -> dict:
    """Run one uncached word analysis (and a DeepL call if selected)."""
    s = settings.load()
    sentence = "Я читаю интересную книгу."
    t0 = time.monotonic()
    result: dict = {"provider": s["llm_provider"], "model": s["llm_model"]}
    try:
        a = analysis.analyse_word(s, "ru", sentence, 18, 24, use_cache=False)
        result["word"] = {"input": "книгу", **a}
        if s["passage_provider"] == "deepl":
            result["deepl"] = llm.deepl_translate(s, ["Hyvää huomenta!"], "fi")[0]
    except llm.BackendError as e:
        return {"ok": False, "error": str(e), **result}
    result["elapsed_ms"] = int((time.monotonic() - t0) * 1000)
    return {"ok": True, **result}
