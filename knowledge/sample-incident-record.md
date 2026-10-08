---
type: note
title: "Incident record skeleton"
repo: gestalt-core
tags: [operations, postmortem]
aliases: [postmortem, outage writeup]
created: 2026-10-08
updated: 2026-10-08
last-verified: 2026-10-08
confidence: medium
---

## What happened ^what-happened

One dated paragraph. State what broke, for how long and who noticed. Leave out blame and speculation. Example: a nightly job filled the disk at 02:10, the search service stopped writing, and the first symptom was an empty result at 08:40.

## Root causes ^root-causes

A numbered list. Each cause carries evidence, such as a command and its output or a file and line. A cause with no evidence is a hypothesis and belongs under open questions.

## Fixes ^fixes

A numbered list. Each fix has a status, either shipped with a commit or deferred with the place where it is tracked. Do not write "will fix". Write who owns it and when it is checked.

## Standing rule ^standing-rule

The rule the incident produced, stated as an instruction. Example: no job writes to the data disk without a size check first. A good rule can be tested. If it cannot be tested, it is a wish.

## What did not ship ^not-shipped

Always present, even when empty. Writing "nothing was deferred" is a claim. Leaving the section out is silence.
