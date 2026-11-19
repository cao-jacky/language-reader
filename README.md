# Language Reader

A small self-hosted reader for learning languages, built around **Russian** and
**Finnish** but configurable to any of about a hundred others. Paste a text,
and its language is detected automatically. Click any word to see its dictionary form, its meaning in that sentence,
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
- **Phrase book.** Every phrase you translate from a selection goes into your
  phrase book, and after translating a passage you can ☆ any sentence to save
  it with its translation. It has its own page in the menu.
- **Vocabulary.** Every looked-up word is stored once per dictionary form, with
  each sentence you met it in. Browse it as a grid of word cards in
  alphabetical sections (A–Ö for Finnish, А–Я for Russian) with a letter index,
  or grouped by CEFR level, status or part of speech; or as a table. Mark words new, learning or known; the reader
  colours them accordingly in every text. Filter by language, status and CEFR
  level, search by word form or meaning, and export to CSV (for Anki or a
  spreadsheet).
- **Your languages.** Add or remove the languages you're learning on the
  Settings page. Pasted text is auto-detected among them with
  [py3langid](https://github.com/adbar/py3langid), offline; text in a language
  you haven't added is named ("looks like Swedish") so you can add it.
  Removing a language keeps its texts and words. Russian and Finnish get
  tailored grammar notes and offline analysers; other languages get general
  guidance (pinyin for Chinese, kana readings for Japanese, stress where it is
  unpredictable) and rely on the model alone.
- **Stronger models on demand.** Pick a default model (Haiku 5.5 out of the
  box) and any number of stronger ones on the same backend and key (Sonnet
  5.5, Opus 5.5). Word, phrase and sentence cards say which model answered
  and offer "Ask Sonnet 5.5" to re-run that one lookup with a stronger model;
  its answer replaces the earlier one (a CEFR level you set by hand is kept).
- **Usage and costs.** Every backend call is logged with its token counts and
  an estimated cost. Settings shows this month and all time, a 30-day chart,
  a breakdown by model and by task, cache hits, and DeepL characters against
  the free quota. Anthropic list prices are built in; set your own for any
  other model (0 for a local one).
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

There is no published image: the compose file builds it from this repository,
so deploying is just pointing Docker at the repo.

### Portainer

1. **Stacks → Add stack → Repository.**
2. Repository URL: `https://github.com/cao-jacky/language-reader`, reference
   `refs/heads/main`, compose path `docker-compose.yml`.
3. Optionally add environment variables (see [Configuration](#configuration)),
   for example `HOST_PORT`, `DATA_PATH` or `AUTH_TOKEN`. None are required.
4. **Deploy the stack.** The first deploy builds the image, which takes a
   minute or two.

Open `http://<host>:8413`, go to **Settings**, choose a backend and press
**Save & test**.

To update, open the stack and use **Pull and redeploy** (or turn on GitOps
updates so Portainer redeploys on each new commit). `pull_policy: build` makes
every deploy rebuild from the latest source, and your data stays in the volume.

By default the database lives in a named volume, `language-reader-data`. Set
`DATA_PATH` to an absolute host folder to keep it somewhere you can back up
directly, and uncomment `user:` in the compose file so the files are owned by
you rather than root.

### Docker Compose

```
git clone https://github.com/cao-jacky/language-reader && cd language-reader
cp .env.example .env      # optional
docker compose up -d --build
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
| `HOST_PORT` | `8413` | Host port (compose only) |
| `DATA_PATH` | named volume | Host folder for `reader.db` (compose only) |
| `WITH_VOIKKO` | `1` | Build with Voikko for Finnish hints (compose only) |
| `DATA_DIR` | `./data` | Folder holding `reader.db` inside the app |
| `PORT` / `HOST` | `8080` / `0.0.0.0` | Listen address |
| `AUTH_TOKEN` | unset | Require this token on every `/api` request. The page asks once and remembers it. |
| `LLM_PROVIDER` | `anthropic` | `anthropic`, `openai` or `none` |
| `LLM_MODEL` | `claude-haiku-5-5` | Default model id |
| `LLM_BASE_URL` | unset | Endpoint override; required for OpenAI-compatible servers other than OpenAI |
| `LLM_API_KEY` / `ANTHROPIC_API_KEY` | unset | Fallback key when none is saved |
| `LLM_EFFORT` | `low` | Claude effort level |
| `PASSAGE_PROVIDER` | `llm` | `llm` or `deepl` |
| `DEEPL_API_KEY` | unset | Fallback DeepL key |
| `TARGET_LANGUAGE` | `English` | Language for glosses and translations |
| `LANGUAGES` | `ru,fi` | Initial languages being learned (ISO 639-1 codes) |

The compose file passes through `AUTH_TOKEN`, `ANTHROPIC_API_KEY` and
`DEEPL_API_KEY`; add any of the others under `environment:` if you want them.

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
