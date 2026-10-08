---
type: note
title: "Session hooks and memory injection"
repo: gestalt-core
tags: [hooks, memory]
aliases: [session start, injection]
created: 2026-10-08
updated: 2026-10-08
last-verified: 2026-10-08
confidence: high
---

## Overview ^overview

A hook is a script the coding agent runs at a fixed moment, such as the start of a session, before a tool call, or when the agent stops. Hooks give the agent memory without the user asking for it.

## What gets injected at session start ^injection

The start hook prints a short block into the agent's context. The block lists open commitments, a few recent session summaries and the memory blocks marked as active. The agent treats it as working state. If the memory service is down, the hook prints nothing and the session proceeds.

## Keeping the block small ^budget

Every injected token is paid for on every turn. The hook caps its output and prefers a pointer over a paste. A pointer names an entry and a block id, and the agent reads the entry only when the task needs it. A long injection that no one reads is worse than none.

## Failure behavior ^failure

A hook must never block the session. Each hook exits zero on any internal error and writes the error to a log file. A guard hook is the one exception. It exits with a blocking code on purpose, and only for the actions it exists to stop.
