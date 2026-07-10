#!/usr/bin/env node
/**
 * compass-tui — live monitor for the claude-compass guard log.
 *
 * Read-only companion: it never touches the hook, its config, or the log.
 * Log path resolution matches the hook exactly: $COMPASS_LOG, else
 * ~/.claude/compass-warns.log.
 */
import os from "node:os";
import path from "node:path";
import React from "react";
import { render } from "ink";
import { App } from "./app.js";

const logPath =
  process.env.COMPASS_LOG ||
  path.join(os.homedir(), ".claude", "compass-warns.log");

render(<App logPath={logPath} />);
