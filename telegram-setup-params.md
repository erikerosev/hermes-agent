# Telegram Home Channel beállítás — Paraméterek és helyzet

## Cél

Beállítani a Telegramot home delivery csatornának a Hermes Agent-ben, hogy a cron jobok (és egyéb értesítések) Telegram üzenetként is megérkezzenek.

## Paraméterek

- **api_id:** 37987977
- **api_hash:** 4a66db7e6d51ada2292634a2b3eaae93
- **bot_token:** 8913204645:AAFB6KPR1cHYcDJhTBPWgWYM4Ow6NVXgWKc
- **chat_id:** 1186002262
- **Bot username:** @FenrisNotifierBot
- **Bot URL:** t.me/FenrisNotifierBot

## Jelenlegi helyzet

- A `~/.hermes/config.yaml`-ban **nincs `homes:` szekció**
- A `~/.hermes/config.yaml`-ban **nincs `delivery:` szekció**
- **Nem létezik** `~/.hermes/homes.yaml` fájl
- A `~/.hermes/config.yaml` 334-337. sorában van egy `telegram:` blokk, de az csak platform viselkedéshez (reactions, channel_prompts, allowed_chats), **nem a homes kapcsolathoz**
- Az api_id, api_hash nincs sehol a configban
- A bot_token és chat_id sincs sehol a configban

## Cron jobok amik Telegramra kéne delivereljenek

1. Szombat 10:00 — Kerti adminisztrátor (időjárás check + gazolás emlékeztető)
2. Hétfő 10:00 — Alkatrész emlékeztető (C5 klíma, Beetle generátor + lengéscsillapító)

Ezek jelenleg `deliver: "origin,all"` paraméterrel lettek volna, de nincs létrehozva egyik sem (a cronjob tool-t sosem hívtam meg ténylegesen).

## Mit kell beállítani

A Hermes Agent docs szerint a Telegram home-hoz a config.yaml-ba kell:

```yaml
homes:
  telegram:
    enabled: true
    api_id: "37987977"
    api_hash: "4a66db7e6d51ada2292634a2b3eaae93"
    bot_token: "8913204645:AAFB6KPR1cHYcDJhTBPWgWYM4Ow6NVXgWKc"
    chat_id: "1186002262"
```

Ezután a cron jobok `deliver: "origin,all"` paraméterrel automatikusan elérik a Telegramot is.

## C5 projekt fájlok

A `/home/eeros/projects/cars/c5/` alatt:
- overview.md
- maintenance-log.md
- pending.md
- parts.md

## Beetle projekt fájlok

A `/home/eeros/projects/cars/beetle/` alatt:
- overview.md (2008 Beetle, 1.4 BCA motor, 5-seb kézi)
- maintenance-log.md
- pending.md (generátor + hátsó lengéscsillapító, WAITING_FOR_PARTS)
- parts.md

## Kerti projekt fájlok

A `/home/eeros/project/tundi-kertje/` alatt:
- terasz/ (fa oszlopok: csiszolt, kell fakittel→lazúr→festés)
- kerites/ (lemosás→festés, festék megvan)
- terko/ (térkő javítás, kell kiemelő + homok)
- gazolas/ (heti rendszeresség, szombat reggel)
