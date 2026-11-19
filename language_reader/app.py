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
import io
import json
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from . import analysis, db, languages, llm, morph, settings, textproc

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
        cur = c.execute(
            "INSERT INTO texts(lang, title, source_url, body) VALUES(?, ?, ?, ?)",
            (lang, title, req.source_url.strip() or None, body),
        )
        tid = cur.lastrowid
        c.executemany(
            "INSERT INTO sentences(text_id, idx, start, end) VALUES(?, ?, ?, ?)",
            [(tid, i, s, e) for i, (s, e) in enumerate(spans)],
        )
    return {"id": tid, "lang": lang}


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
                "SELECT o.id, o.lemma_id, o.start, o.end, o.surface, l.status "
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
    if not req.force:
        rows = [r for r in rows if r["translation"] is None]
    if rows:
        s = settings.load()
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
    s = settings.load()
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
        if row:
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
        "surface": surface,
        "lemma": _lemma_dict(lrow),
        "sentence": {"id": sent["id"], "text": sentence, "translation": sent["translation"]},
    }


class PhraseReq(BaseModel):
    text_id: int
    start: int
    end: int


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
    s = settings.load()
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
    return dict(row)


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
    return {"ok": True}


# --------------------------------------------------------------------------- #
# vocabulary
# --------------------------------------------------------------------------- #

_VOCAB_SQL = """
    SELECT l.*, COUNT(o.id) AS occurrences, MAX(o.created_at) AS last_seen,
           (SELECT o2.sentence_text FROM occurrences o2 WHERE o2.lemma_id = l.id ORDER BY o2.id LIMIT 1) AS example,
           (SELECT o2.surface FROM occurrences o2 WHERE o2.lemma_id = l.id ORDER BY o2.id LIMIT 1) AS example_surface,
           (SELECT s.translation FROM occurrences o2 JOIN sentences s ON s.id = o2.sentence_id
              WHERE o2.lemma_id = l.id ORDER BY o2.id LIMIT 1) AS example_translation
    FROM lemmas l LEFT JOIN occurrences o ON o.lemma_id = l.id
    {where}
    GROUP BY l.id
    ORDER BY COALESCE(MAX(o.created_at), l.updated_at) DESC
"""


def _vocab_rows(lang: str, status: str, cefr: str, q: str) -> list[dict]:
    where, args = [], []
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
def list_vocab(lang: str = "", status: str = "", cefr: str = "", q: str = "") -> list[dict]:
    return _vocab_rows(lang, status, cefr, q)


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
    return {"ok": True}


@app.get("/api/export.csv")
def export_csv(lang: str = "", status: str = "", cefr: str = "") -> Response:
    rows = _vocab_rows(lang, status, cefr, "")
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["lang", "lemma", "stressed", "pos", "gloss", "other_meanings", "cefr", "status",
                "example", "example_translation", "notes"])
    for r in rows:
        w.writerow([r["lang"], r["lemma"], r["stressed"], r["pos"], r["gloss"], "; ".join(r["other_meanings"]),
                    r["cefr"] or "", r["status"], r["example"] or "", r["example_translation"] or "", r["notes"] or ""])
    name = f"vocabulary-{lang or 'all'}-{time.strftime('%Y%m%d')}.csv"
    return Response(
        "﻿" + buf.getvalue(),  # BOM so spreadsheet apps read UTF-8
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


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
