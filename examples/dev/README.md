# Mobster for developers: a starter kit

Copy these into your app's repository to run Mobster's checks in CI, after Xcode builds, for an Expo app, and to make App Store screenshots. Everything runs on your own Mac. [docs.mobster.dev/ci](https://docs.mobster.dev/ci) has the whole story.

| File | Where it goes | What it does |
| --- | --- | --- |
| `github/mobster.yml` | `.github/workflows/mobster.yml` | Runs `mobster ci run` on every pull request on a self-hosted Mac runner labelled `mobster`, uploads `.mobster/ci/` and keeps one comment with the verdict, repro steps and video. `mobster ci init` writes the same file. |
| `gitlab/mobster.gitlab-ci.yml` | `.gitlab-ci.yml`, or included from it | The same for GitLab merge requests, with the JUnit report in the merge request. Needs `MOBSTER_GITLAB_TOKEN`. |
| `checks/home.yaml` | `.mobster/checks/home.yaml` | A launch-only check tagged `smoke` and `screenshot`: CI runs it, `mobster xcode hook --tag smoke` runs it after each build, and `mobster asc screenshots` makes a store screenshot from it. |

Then, on your Mac:

```sh
mobster ci doctor                                   # is this Mac ready to be the runner?
mobster xcode hook --scheme YourApp --tag smoke     # checks after every simulator build
mobster expo preview --device "Test iPhone"         # an Expo app: open it on your iPhone from Metro
mobster asc key add --key-id ID --issuer-id UUID --p8 AuthKey_ID.p8
mobster asc screenshots --size 6.9 --size 13        # App Store screenshots in every language
```
