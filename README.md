# feature-flag-staleness-scanner

Your codebase is full of feature flags that finished rolling out months ago — and nobody cleaned them up. Every stale flag is a dead code path that doubles the branches you have to reason about, test, and keep compiling.

This is a zero-dependency CLI that statically scans your code for flag usage (LaunchDarkly, Unleash, OpenFeature, Statsig, env-var flags, YAML/JSON config flags), compares against the rollout states **you** provide, and tells you which flags are **STALE** (100% rolled out but still in code), **ZOMBIE** (untouched in 90+ days), or **ORPHAN** (in code, missing from your states file). Exits 1 on any STALE flag, so it gates CI on flag hygiene.

## Quickstart (30 seconds)

```bash
# 1. Write your rollout states (flag -> rollout % or "retired")
cat > flags.json <<'EOF'
{
  "new_checkout_flow": 100,
  "holiday_banner_2024": 100,
  "FEATURE_GIFT_WRAPPING": 30
}
EOF

# 2. Scan
python3 flag_scanner.py ./your-repo --states flags.json
```

No installs, no dependencies — Python 3.9+ stdlib only.

## Example output

Real output from a real run against a demo repo (exit code was `1` because STALE flags were found):

```
feature-flag-staleness-scanner report
flags found in code: 3

STALE (2) - 100% rollout or retired, still referenced:
  - holiday_banner_2024 [rollout 100.0%] -> shop/app.py:13
  - new_checkout_flow [rollout 100.0%] -> shop/app.py:8

ZOMBIE (2) - untouched for 90+ days:
  - holiday_banner_2024 [last touched 715d ago] -> shop/app.py:13
  - new_checkout_flow [last touched 715d ago] -> shop/app.py:8
```

`FEATURE_GIFT_WRAPPING` (30% rollout, touched recently) correctly stays silent.

A clean repo reports `clean - no stale, zombie, or orphan flags found.` and exits `0`.

## Options

```
python3 flag_scanner.py PATH --states flags.json [--zombie-days 90] [--format json]
```

- `--states` (required): JSON mapping flag key → rollout percent (`0`–`100`), `"retired"`, or `{"rollout": N}`.
- `--zombie-days`: days without a git touch before a flag counts as ZOMBIE (default 90).
- `--format json`: machine-readable report for pipelines.

## Detected flag patterns

| Provider | Example matched |
|---|---|
| LaunchDarkly | `variation("flag-key")`, `variationDetail("flag-key")` |
| Unleash | `isEnabled("flag")` |
| OpenFeature | `getBooleanValue("flag")`, `get_boolean_value("flag")`, … |
| Statsig | `checkGate("flag")`, `check_gate("flag")`, `getConfig("flag")` |
| Env / config | `os.getenv("FEATURE_X")`, `process.env.FEATURE_X`, flags in `.yaml`/`.json` |
| Generic helpers | `isFeatureEnabled("flag")`, `is_feature_enabled("flag")`, … |

Zombie detection uses `git log -S` on the flag key and ignores your states file itself (committing the states file is bookkeeping, not a real touch). Outside a git repo it skips gracefully with a note.

## What it does NOT do

- It does **not** read rollout state from LaunchDarkly/Unleash/etc. — your states file is the source of truth. (Reading live state would need API keys and accounts; this tool stays zero-dependency on purpose.)
- It does **not** delete or rewrite code. It reports; you clean up.
- It does **not** understand dynamic flag keys (e.g. `variation("prefix_" + name)`). Only string literals are detected — this is a static scanner.
- YAML parsing is best-effort (stdlib only, no PyYAML): nested keys are captured as written.
- Zombie detection is only as good as your git history: squashed monorepo imports or shallow clones weaken the signal.

## Tests

```bash
python -m unittest discover -s tests -v   # 8 tests, all green
```

Tests build real fixture git repos in tmp with backdated commits and assert STALE/ZOMBIE/ORPHAN are detected exactly, plus a clean-fixture clean report.

## License

MIT — see [LICENSE](LICENSE).

## Built by Ingenious AI

This tool is part of the Ingenious AI security platform suite:

- [DisclosureGuard](https://wa.me/5016691053?text=Hi%20Ingenious%20AI%21%20I%20want%20early%20access%20to%20DisclosureGuard.)
- [ModelMinder](https://wa.me/5016691053?text=Hi%20Ingenious%20AI%21%20I%20want%20early%20access%20to%20ModelMinder.)
- [TermsWatch](https://wa.me/5016691053?text=Hi%20Ingenious%20AI%21%20I%20want%20early%20access%20to%20TermsWatch.)
- [Vibe-Code Audit](https://wa.me/5016691053?text=Hi%20Ingenious%20AI%21%20I%20want%20early%20access%20to%20Vibe-Code%20Audit.)
- [Rogue-Agent Defense](https://wa.me/5016691053?text=Hi%20Ingenious%20AI%21%20I%20want%20early%20access%20to%20Rogue-Agent%20Defense.)
- [BreachHunter](https://wa.me/5016691053?text=Hi%20Ingenious%20AI%21%20I%20want%20early%20access%20to%20BreachHunter.)
- [ChatBreaker](https://wa.me/5016691053?text=Hi%20Ingenious%20AI%21%20I%20want%20early%20access%20to%20ChatBreaker.)
- [Agent Flight Recorder](https://wa.me/5016691053?text=Hi%20Ingenious%20AI%21%20I%20want%20early%20access%20to%20Agent%20Flight%20Recorder.)
- [Agent Auth Gateway](https://wa.me/5016691053?text=Hi%20Ingenious%20AI%21%20I%20want%20early%20access%20to%20Agent%20Auth%20Gateway.)
- [DomainGuard](https://wa.me/5016691053?text=Hi%20Ingenious%20AI%21%20I%20want%20early%20access%20to%20DomainGuard.)
- [ContentCraft AI](https://wa.me/5016691053?text=Hi%20Ingenious%20AI%21%20I%20want%20early%20access%20to%20ContentCraft%20AI.)

