# Paired development evaluation

Create a schema 1 manifest for your own paired bugs. Each case needs a unique `id`, a `family` (`automation`, `testing`, `ux`, or `reliability`), a `checkSha256`, `bugCheck`, nonempty `regressionChecks` and `criticalChecks`, and `maxAttempts` from 1 to 10. The manifest pins the bug predicate and unchanged regression and critical predicates. It contains no measured results or build IDs.

Run the scorer from a source checkout with Python 3.12. It is offline:

```sh
python -m mobile_agent.evals.development.score --manifest <manifest.json> <run-directory>
```

It reads `<run-directory>/claims.json` and `oracle.json` and prints a JSON report. Both files have `schema: 1`, `manifestSha256` (hash of the exact manifest bytes), the same nonempty `runId` and `configSha256`, and an `attempts` array. `oracle.json` also has distinct `baselineBuildSha256` and `fixedBuildSha256`. The runner must keep build labels and oracle results hidden from the agent while it works.

Each attempt has `caseId` and a positive, contiguous `attempt` number. A claim has `baselineVerdict` (`bug`, `clean`, `unknown`) and `fixedVerdict` (`fixed`, `broken`, `unknown`). An oracle attempt has `baseline` and `fixed` arms. Each arm has the matching `buildSha256`, equal `startStateSha256` and `setupSha256` across the pair, the manifest's `checkSha256` as `assertionSha256`, and `reached` (`true`, `false`, or `null`). For a reached arm, `checks` names the manifest's predicates. A check has `id`, boolean or null `ok`, a relative `evidence` artifact path inside the run directory, and its `sha256`. The harness writes oracle observations independently of the agent's claims. The setup digest covers the simulator configuration and seed, excluding the registered bug delta; the assertion digest covers the unchanged check definition on both builds.

An agent can attach `repro: {"path": "repro.txt", "sha256": "..."}` to its claim. The trusted harness reruns that exact artifact on both builds and records `replayedReproSha256` on its oracle attempt; the paired arm observations then come from that replay. `reproProven` needs the artifact bytes, harness digest, matched setup, baseline failure, and fixed success. It is separate from `verifiedFix`: a reproduction can be proven while a critical safety failure vetoes the fix. The scorer does **not** claim the reproduction is minimal: minimality needs a reduction rule and evidence that removing steps loses the failure.

The report includes every registered case even when neither file has an attempt. A baseline bug earns **detected** only when independent evidence shows the bug and the agent reports it. An exposed bug the agent misses is **survived**. A baseline not reached is **unreached**; a missing, mismatched, or untrusted observation is **inconclusive**. The report distinguishes first-attempt detection from eventual recovery. It counts disproved and unproved success **claims** (up to two per attempt), and false alarms on the clean fixed arm separately.

`verifiedFix` requires a detected baseline bug, a clean fixed arm, the same registered assertion and setup, unchanged regression checks on both arms, and an agent fixed verdict. Any attempted pair with a failed or unknown critical check permanently vetoes it. A later failed or unknown paired check also vetoes an earlier proof. Missing evidence cannot be turned into a negative oracle or excluded from the denominator.

This scorer validates file hashes and joins; it cannot authenticate who wrote `oracle.json` or prove a hardware observation was genuinely independent. A trusted harness must capture the artifacts and build digests, enforce agent blinding, and preserve its provenance. Until then, this corpus is a pre-registered evaluation design, not a measured performance result. Hardware evaluations belong in a separately scheduled run, never `make test-backend`.
