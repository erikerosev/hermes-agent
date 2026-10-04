# Apollo migráció: v2026.7.20 (0.19.0) → v2026.9.24 (0.21.5)

Bázis: `v2026.9.24`, branch `apollo-v2026-09`. A régi fork 31 commitja a
`v2026.7.20..apollo-v2026` sávban. A változásaink additívak voltak
(6853 beszúrás / 91 törlés / 59 fájl), ezért ez nem merge, hanem **újra-landolás**.

## 0. Ami feleslegessé vált (NE portoljuk)

| ami a miénk | miért nem kell |
|---|---|
| `agent/claude_cli_runner.py` (899) | a `claude-subscription-directsdk` plugin váltja |
| `agent/claude_process_manager.py` (661) | ua. — Hermes birtokolja a loopot, nincs pool/priming |
| `agent/thread_summary.py` (246) | upstream `context_compressor_summary` + `conversation_compression_archive` |
| `agent/claude_resume_store.py` (81) | a plugin kérésenként friss processzt indít |
| `agent/transports/claude_cli.py` (262) | a plugin model-provider, nem transport |
| `claude_code_core/*` (525) | a plugin maga parse-olja a stream-jsont |
| `plugins/model-providers/claude-cli/*` (35) | ua. |
| **pinned-session no-compression** (`4382cf6a1e`) | **upstream rendesen megjavította (#98619/#13437/#15000)**: `_resolve_live_session_id()` → `db.resolve_resume_session_id()` a rotáció előtti kliens-id-t a friss csúcsra oldja fel. A mi kerülőnk ÁRA volt a 2M karakteres szál. |
| stall-watchdog a claude-pool olvasóhurkán | a pool megszűnik; upstream `activity_tracking.py` + turn-liveness watchdog |
| aux timeout-breaker (160 sor) | upstream `auxiliary_health.py` ok-tudatos karantén (rate limit / connection / invalid response → 60 s; payment/quota → hosszú TTL). Ha a timeout mint ok hiányzik, az 1 soros upstream-alakú bővítés, nem 160 soros patch. |
| 2000 karakteres üzenet-vágás (`c2acbd77cd`) | a priming megszűnik |
| duplikált user-üzenet fix (`39e92cd364`) | claude-cli-specifikus |
| cwd / „nem kódolási feladat" (`f8aaa20adc`, `4ed06b9b6f`) | a plugin saját rendszerpromptot ad, native settings off |
| `HERMES_ATLASSIAN_MCP=0` truthiness (`ed3a7f2294`) | claude-cli env-híd, megszűnik |
| 10 db „port stage" commit | az előző rebase munkája, csak a claude-cli landolásához kellett |

## 1. Portolandó (a megmért tudás, ami nem a claude-cli-ről szól)

- [ ] `gateway/platforms/api_server.py` SSE: `hermes.thinking`, `hermes.answer_clarify`,
      `hermes.complete`, `hermes.close`, `hermes.stream_turn` + `X-Hermes-Tool-Progress`
      header → **upstream `api_server_openai_routes.py`-ba**, mert oda került az OpenAI-út.
      (`hermes.tool.progress` és `hermes.status` MÁR upstreamen van, azonos payloaddal.)
- [ ] `plugins/web/curl/*` (346) — kulcs nélküli web_extract
- [ ] `plugins/image_gen/comfyui/*` (352)
- [ ] `hermes_cli/kanban_review.py` (191)
- [ ] `hermes_gatelog.py` (52) — kapu-napló. Upstream `hermes_cli/observability/` MÁS
      (shared-metrics relay), nem váltja ki.
- [ ] `tools/request_approval_tool.py` (162) — clarify
- [ ] aux-routing: `HERMES_AUX_BASE_URL` / `_API_KEY` / `_API_MODE` / `apollo-aux` sentinel
      (az aux-rekurzió fix; a breaker NEM kell)
- [ ] `tools/web_tools.py` registry-fallthrough + `url` alias
- [ ] `tools/terminal_tool.py` anti-hamis-siker
- [ ] `tools/skill_manager_tool.py` + `agent/curator.py` overlap-gate + kvóta-fix
- [ ] `tools/kanban_tools.py` leírás-korlát + `hermes_cli/kanban*.py`
- [ ] `plugins/platforms/slack/adapter.py` always-cc
- [ ] `toolsets.py`, `agent/prompt_builder.py`, `agent/conversation_loop.py` bekötések

## 2. Plugin-oldal (nem kód, konfiguráció)

- [ ] `claude-subscription-directsdk` telepítése **eldobható HERMES_HOME-mal**
- [ ] MCP-moat: a plugin „MCP advertises only the current Hermes tool inventory" →
      a `hermes_tools_mcp_server` híd feleslegessé válhat; a kozos/behavior/reminder/
      atlassian/peak-files szerverek viszont Hermes-tool oldalra kerülnek
- [ ] A/B mérés a mai úttal: **latencia + cache-hit** (ez a két szám dönt)

## 3. Nyitott kérdések

- A Claude Code saját skill-rendszere kiesik; a Hermes sajátja ugyanazt a
  `~/.hermes/skills/` könyvtárat olvassa, tehát a 123 skill átjön. **Ellenőrizve.**
- CLI-verzió: a plugin 2.1.263-ra kvalifikált, mi 2.1.276-ot futtatunk. Nincs
  verzió-kapu a kódban, de nem tesztelt.
- Friss processz kérésenként → latencia-regresszió lehet. Mérni kell.
