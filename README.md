# Language Reader

A small self-hosted reader for learning **Russian** and **Finnish**. Paste a
text, click any word to see its dictionary form, its meaning in that sentence,
its grammatical form and an estimated CEFR level, select a few words to
translate them as a phrase, or translate the whole passage sentence by
sentence. Every word and phrase you look up is kept in a SQLite database, so
your vocabulary builds up as you read.

## What it does

- **Click a word.** You get the lemma (кни́гу → **кни́га**, *talossa* → **talo**),
  part of speech, the form it is in ("accusative singular, feminine",
  "inessive (-ssa), singular"), its meaning in context, other common meanings
  and a short note: verb aspect pairs for Russian, verb type and consonant
  gradation for Finnish, and compound parts (*rautatieasema* → rauta + tie +
  asema). Russian lemmas show stress marks.
- **Select a phrase.** You get a natural translation, a literal one where it
  differs, and a note on any idiom or construction.
- **Translate a passage.** Sentences are translated in batches and shown side
  by side with the original.
- **Vocabulary.** Every looked-up word is stored once per dictionary form, with
  each sentence you met it in. Mark words new, learning or known; the reader
  colours them accordingly in every text. Filter by language, status and CEFR
  level, search by word form or meaning, and export to CSV (for Anki or a
  spreadsheet).
- **Settings page.** Choose the backend, model, base URL and API key in the
  browser. No restart needed.

## Backends

| Backend | Use it for | Notes |
|---|---|---|
| **Claude API** | Default: `claude-haiku-5-5` at effort `low` | About $0.0001 per word lookup and $0.001 per 500-word passage. Answers use JSON-schema structured outputs, so they always parse. |
| **OpenAI-compatible** | Ollama, LM Studio, vLLM, llama.cpp, OpenRouter, OpenAI | Anything with a `/v1/chat/completions` endpoint. From inside Docker, reach a server on the host at `http://host.docker.internal:11434/v1`. Small local models handle Russian better than Finnish. |
| **DeepL** | Whole passages only (optional) | The free tier covers 500k characters a month. Keys ending in `:fx` use the free endpoint automatically. |

Every answer is cached by backend, model and input, so re-reading a text or
re-clicking a word costs nothing. The ↻ button on a word card re-asks the model
and skips the cache.

### Offline analysers

[pymorphy3](https://github.com/no-plagiarism/pymorphy3) (Russian) and
[Voikko](https://voikko.puimula.org/) (Finnish) run locally. When installed,
their candidate dictionary forms are sent to the model as hints, which makes
lemmas noticeably more reliable on smaller models. The Docker image includes
both; the Settings page shows whether they loaded.

### CEFR levels

There is no official CEFR word list for Finnish, and Russian has only the TORFL
lexical minimums. For now each level is the model's estimate and is labelled
"LLM estimate". You can correct a level in the vocabulary table, and the source
then becomes "manual".

## Running it

### Docker Compose

```
cp .env.example .env      # set DATA_PATH to a folder for reader.db
docker compose up -d
```

Open <http://localhost:8413>, go to **Settings**, choose a backend and press
**Save & test**.

To build the image from source instead of pulling it from GHCR:

```
docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build
```

### Directly

```
uv venv && uv pip install -r requirements.txt
DATA_DIR=./data .venv/bin/python -m language_reader      # http://localhost:8080
```

For Finnish hints outside Docker, install Voikko (`brew install libvoikko` or
`apt install libvoikko1 voikko-fi`) and `pip install libvoikko`.

## Configuration

Everything about the backend is set on the Settings page. Environment variables
only provide defaults for a fresh install; values saved in the page win.

| Variable | Default | Purpose |
|---|---|---|
| `DATA_DIR` | `./data` | Folder holding `reader.db` |
| `PORT` / `HOST` | `8080` / `0.0.0.0` | Listen address |
| `AUTH_TOKEN` | unset | Require this token on every `/api` request. The page asks once and remembers it. |
| `LLM_PROVIDER` | `anthropic` | `anthropic`, `openai` or `none` |
| `LLM_MODEL` | `claude-haiku-5-5` | Model id |
| `LLM_BASE_URL` | unset | Endpoint override; required for OpenAI-compatible servers other than OpenAI |
| `LLM_API_KEY` / `ANTHROPIC_API_KEY` | unset | Fallback key when none is saved |
| `LLM_EFFORT` | `low` | Claude effort level |
| `PASSAGE_PROVIDER` | `llm` | `llm` or `deepl` |
| `DEEPL_API_KEY` | unset | Fallback DeepL key |
| `TARGET_LANGUAGE` | `English` | Language for glosses and translations |

API keys saved on the Settings page are stored in `reader.db` and never returned
to the browser (only their last four characters). Back up or protect the data
folder accordingly.

## API

The page is a thin client over a JSON API; interactive docs are at
`/api/docs`. The main endpoints:

| Method | Path | |
|---|---|---|
| `GET` / `POST` | `/api/texts` | List or add texts |
| `GET` / `PATCH` / `DELETE` | `/api/texts/{id}` | A text with sentences, tokens, lookups and phrases |
| `POST` | `/api/texts/{id}/translate` | Translate all or selected sentences |
| `POST` | `/api/lookup` | Analyse the word at `start`–`end` in a text and save it |
| `GET` / `POST` / `DELETE` | `/api/phrases` | Phrase translations |
| `GET` / `PATCH` / `DELETE` | `/api/vocab[/{id}]` | Vocabulary, filterable by `lang`, `status`, `cefr`, `q` |
| `GET` | `/api/export.csv` | Vocabulary as CSV |
| `GET` / `PUT` | `/api/settings` | Backend settings |
| `POST` | `/api/settings/test` | Run one uncached lookup against the configured backend |

## Licence

MIT
