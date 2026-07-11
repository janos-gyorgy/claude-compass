#!/usr/bin/env node
/**
 * compass-tui — live monitor for the claude-compass guard log, plus a config
 * pane (press c) that toggles rule groups in the armed compass.toml.
 *
 * Never touches the hook or the log. Config edits are line-surgical
 * (enabled/action only), atomic, and leave a .bak. Path resolution matches
 * the hook exactly: $COMPASS_LOG / $COMPASS_CONFIG, else ~/.claude/.
 */
import os from "node:os";
import path from "node:path";
import React from "react";
import { render } from "ink";
import { App } from "./app.js";
import { configPath } from "./config.js";

const logPath =
  process.env.COMPASS_LOG ||
  path.join(os.homedir(), ".claude", "compass-warns.log");

render(<App logPath={logPath} confPath={configPath()} />);
