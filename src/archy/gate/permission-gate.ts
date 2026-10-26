/**
 * The permission gate.
 *
 * ARCHY hands the model four tools: read, write, edit and bash. Before this
 * file existed, a sentence in the system prompt was the only thing standing
 * between a wrong answer and `rm -rf`, and a sentence is not a boundary: it is a
 * suggestion to a text predictor that may have just read a webpage telling it to
 * ignore its instructions. Pi 1.4.0 can refuse a tool call in code, so the
 * refusal lives here, where the model cannot argue with it.
 *
 * Loaded only when the owner turned it on. ARCHY passes `--no-extensions` and
 * names this file with `-e`, so extension discovery stays off (no MCP handshake
 * per turn) while this one file still loads.
 *
 * Three things this file deliberately does not do:
 *
 *   - It never asks a question. ARCHY runs pi in rpc mode over a pipe, and
 *     `ctx.ui` there has no one to answer, so a confirmation prompt would hang
 *     the turn until its deadline. A blocked action is written to the pending
 *     file instead, and ARCHY is what asks the owner, out loud or from a
 *     terminal.
 *   - It never throws to allow something. Pi blocks a tool call when a handler
 *     fails, so an error here is a block by another name, but the reason would be
 *     lost, so errors are turned into a refusal that says what broke.
 *   - It never reads a policy it does not trust. A missing, empty, or malformed
 *     policy denies everything: an unreadable rulebook is not permission.
 */

import { createHash } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, realpathSync, renameSync, writeFileSync } from "node:fs";
import { dirname, isAbsolute, join, normalize, resolve, sep } from "node:path";

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

export interface Policy {
	version?: number;
	enabled?: boolean;
	roots?: unknown;
	readonly_commands?: unknown;
	approvals?: string;
	pending?: string;
	log?: string;
	approval_ttl_s?: number;
	/** Whether the model answering right now is somebody else's computer. */
	privacy_remote?: boolean;
	/** Whether file contents may go to that model at all. */
	privacy_allow_files?: boolean;
}

export interface Decision {
	allow: boolean;
	reason?: string;
	digest?: string;
	/** A valid approval was presented for exactly this action. */
	approved?: boolean;
	/** The owner should be asked. False when the call is wrong, not merely bold. */
	approval?: boolean;
	/** False when the gate is switched off, so callers never report enforcement. */
	enforcing?: boolean;
}

/** A policy file that cannot be read or understood. Denies everything. */
interface Unusable {
	unusable: string;
}

type Loaded = Policy | Unusable;

const POLICY_ENV = "ARCHY_PERMISSION_POLICY";

/**
 * Commands whose *name* is enough to know they only look. Every one of these is
 * still checked per argument below, because `find -delete` and `git config` are
 * both written with a harmless first word.
 */
const READONLY_COMMANDS = new Set([
	"ls", "cat", "head", "tail", "wc", "stat", "file", "tree", "df", "du", "free",
	"uptime", "date", "uname", "whoami", "id", "hostname", "pwd", "echo", "which",
	"type", "command", "nvidia-smi", "pgrep", "ps", "rg", "grep", "fd", "find",
	"sort", "uniq", "cut", "tr", "jq", "git", "systemctl", "journalctl", "pacman",
	"niri", "busctl", "env", "printenv",
]);

/**
 * Words that make a line unsafe wherever they appear, including after a
 * semicolon and including when the owner's config tries to allow them. This is
 * the hard line: an install can add a command to the read-only list, it cannot
 * promote `sudo` or `rm` into it, and a sentence inside a command is not a
 * separate command.
 */
const NEVER_READONLY = new Set([
	"sudo", "doas", "pkexec", "su", "rm", "rmdir", "mv", "cp", "install", "dd",
	"mkfs", "mkfs.ext4", "fdisk", "parted", "chmod", "chown", "chgrp", "shred",
	"wipefs", "truncate", "tee", "kill", "pkill", "killall", "sh", "bash", "zsh",
	"dash", "eval", "exec", "source", "curl", "wget", "nc", "ncat", "socat",
	"apt", "apt-get", "dnf", "yum", "zypper", "systemd-run", "mount", "umount",
	"modprobe", "insmod", "reboot", "shutdown", "poweroff", "halt", "init",
	"python", "python3", "python3.14", "node", "deno", "bun", "perl", "ruby",
	"openssl", "gpg", "useradd", "usermod", "passwd", "crontab", "at", "systemctl-restart",
]);

/** The tools that carry the contents of a file into a request. */
const FILE_TOOLS = new Set(["read", "write", "edit"]);

/**
 * Flags that turn a looking command into a writing or an executing one.
 *
 * `-x` and `--pre` are here for the same reason `-exec` is: `fd -x rm` and
 * `rg --pre thing` run another program, and the program they run does not have to
 * be one this list has heard of. A name-only check cannot see that, so the flag
 * that carries it is refused instead. Measured before the fix: `env /home/x.sh`,
 * `command -p /home/x` and `fd -x /home/x` were all allowed.
 */
const WRITING_FLAGS = [
	/^-i$/, /^--in-place/, /^-delete$/, /^-exec(dir)?$/, /^-fprint(f)?$/, /^-fls$/,
	/^--vacuum/, /^--force$/, /^-s$/, /^-x$/, /^-X$/, /^--exec(-batch)?$/,
	/^-ok(dir)?$/, /^--pre$/, /^--rotate$/, /^--sync$/,
];

/**
 * Flags that only some commands write with, because the same letter is harmless
 * elsewhere: `ls -o` is a listing, `sort -o file` writes one.
 */
const COMMAND_FLAGS: Record<string, RegExp[]> = {
	sort: [/^-o$/, /^--output(=.*)?$/],
	journalctl: [/^--setup-keys$/, /^--relinquish-var$/],
};

/**
 * Commands that run another program from their arguments. Read-only only while
 * they are listing: `env` alone prints the environment, `env anything` runs it.
 */
const PASS_THROUGH = new Set(["env", "command"]);

/**
 * niri's `msg` read-outs. `niri msg action ...` is a change (it closes windows,
 * moves workspaces, quits the compositor), which is why only the listing
 * subcommands are here: window work in this assistant is done in code.
 */
const NIRI_MSG_READS = new Set(["windows", "outputs", "workspaces", "layers",
	"focused-window", "focused-output", "keyboard-layouts", "version", "validate"]);

/** Second words that are allowed for commands whose second word decides. */
const SUBCOMMANDS: Record<string, Set<string>> = {
	busctl: new Set(["list", "tree", "status", "introspect", "get-property",
		"get-property-type", "capture", "help", "--version"]),
	git: new Set(["status", "log", "diff", "show", "branch", "remote", "rev-parse",
		"describe", "ls-files", "blame", "shortlog", "tag", "whatchanged", "grep",
		"count-objects", "version"]),
	systemctl: new Set(["status", "is-active", "is-enabled", "is-failed", "show",
		"cat", "list-units", "list-timers", "list-sockets", "get-default", "--version"]),
	pacman: new Set(["--version", "--help", "-Qs", "-Qi", "-Ql", "-Qo", "-Q", "-Ss", "-Si", "-Sl"]),
};

const MAX_COMMAND = 4096;
const MAX_SUMMARY = 200;
const MAX_PENDING = 20;

// --------------------------------------------------------------- the policy

/** Read and check the policy. Anything wrong with it makes it unusable. */
export function loadPolicy(path?: string): Loaded {
	const file = path ?? process.env[POLICY_ENV];
	if (!file) {
		return { unusable: `no policy file was named in ${POLICY_ENV}` };
	}
	let raw: string;
	try {
		raw = readFileSync(file, "utf8");
	} catch (error) {
		return { unusable: `the policy file could not be read: ${(error as Error).message}` };
	}
	if (!raw.trim()) return { unusable: "the policy file is empty" };
	let parsed: unknown;
	try {
		parsed = JSON.parse(raw);
	} catch {
		return { unusable: "the policy file is not JSON" };
	}
	if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
		return { unusable: "the policy is not an object" };
	}
	const policy = parsed as Policy;
	if (policy.enabled === false) return policy;         // switched off on purpose
	if (!Array.isArray(policy.roots) || policy.roots.some((root) => typeof root !== "string")) {
		// With no roots, every path is outside them, and this must read as a
		// broken rulebook rather than as a rule that happens to allow nothing.
		return { unusable: "the policy has no usable list of approved directories" };
	}
	return policy;
}

function isUnusable(policy: Loaded): policy is Unusable {
	return typeof (policy as Unusable).unusable === "string";
}

// ------------------------------------------------------------- what is a call

/** A call reduced to what it actually does, or a reason it makes no sense. */
interface Call {
	tool: string;
	/** Canonical form, used for the digest so the same action always hashes the same. */
	action: string;
	summary: string;
	path?: string;
	command?: string;
}

function bad(why: string): { invalid: string } {
	return { invalid: why };
}

/** Resolve a path the way the kernel will, so `..` and symlinks cannot hide. */
function realOf(target: string): string {
	const absolute = resolve(target);
	let probe = absolute;
	const tail: string[] = [];
	// realpath only works on what exists, so walk up to the first thing that does.
	while (!existsSync(probe)) {
		const parent = dirname(probe);
		if (parent === probe) return normalize(absolute);
		tail.unshift(probe.slice(parent.length + 1));
		probe = parent;
	}
	// The native one expands Windows 8.3 short names and the JavaScript one does
	// not, so a temp directory named RUNNER~1 resolved to one form for the target
	// and another for the root, and a file inside an approved directory was judged
	// to be outside it. Native first, plain as the fallback.
	for (const realpath of [realpathSync.native, realpathSync]) {
		try {
			return join(realpath(probe), ...tail);
		} catch {
			continue;
		}
	}
	return normalize(absolute);
}

/** Windows compares paths without case, and everywhere else with it. */
function samePath(left: string, right: string): boolean {
	if (process.platform !== "win32") return left === right;
	return left.toLowerCase() === right.toLowerCase();
}

function insideRoots(target: string, roots: string[]): boolean {
	const real = realOf(target);
	for (const root of roots) {
		const realRoot = realOf(root);
		if (samePath(real, realRoot)) return true;
		const prefix = realRoot.endsWith(sep) ? realRoot : realRoot + sep;
		if (samePath(real.slice(0, prefix.length), prefix)) return true;
	}
	return false;
}

function isString(value: unknown): value is string {
	return typeof value === "string";
}

function canonicalise(tool: string, input: unknown, cwd: string): Call | { invalid: string } {
	if (typeof input !== "object" || input === null || Array.isArray(input)) {
		return bad("the arguments were not an object");
	}
	const args = input as Record<string, unknown>;
	const path = (value: unknown, field: string) => {
		if (!isString(value) || !value.trim()) return bad(`no usable ${field} was given`);
		if (value.includes("\0")) return bad(`the ${field} contains a null byte`);
		return { path: realOf(isAbsolute(value) ? value : resolve(cwd, value)) };
	};

	if (tool === "read" || tool === "ls") {
		const raw = args.path;
		const resolved = raw === undefined && tool === "ls" ? { path: realOf(cwd) } : path(raw, "path");
		if ("invalid" in resolved) return resolved;
		if (args.offset !== undefined && typeof args.offset !== "number") {
			return bad("the offset is not a number");
		}
		if (args.limit !== undefined && typeof args.limit !== "number") {
			return bad("the limit is not a number");
		}
		return { tool, path: resolved.path, action: `path:${resolved.path}`,
			summary: resolved.path };
	}

	if (tool === "write" || tool === "edit") {
		const resolved = path(args.path, "path");
		if ("invalid" in resolved) return resolved;
		if (tool === "write") {
			if (!isString(args.content)) return bad("no content was given to write");
			return { tool, path: resolved.path, action: `path:${resolved.path}`,
				summary: `${resolved.path} (${Buffer.byteLength(args.content)} bytes)` };
		}
		if (!Array.isArray(args.edits) || args.edits.length === 0) {
			return bad("no edits were given");
		}
		for (const edit of args.edits) {
			if (typeof edit !== "object" || edit === null) return bad("an edit was not an object");
			const { oldText, newText } = edit as Record<string, unknown>;
			if (!isString(oldText) || !isString(newText)) {
				return bad("an edit did not have both an old and a new text");
			}
		}
		const bytes = args.edits.reduce(
			(total: number, edit) => total + Buffer.byteLength((edit as { newText: string }).newText), 0);
		return { tool, path: resolved.path, action: `path:${resolved.path}`,
			summary: `${resolved.path} (${args.edits.length} edits, ${bytes} bytes)` };
	}

	if (tool === "bash") {
		if (!isString(args.command)) return bad("no command was given");
		if (!args.command.trim()) return bad("the command was empty");
		if (args.command.length > MAX_COMMAND) return bad("the command is longer than this assistant should run");
		if (args.timeout !== undefined && typeof args.timeout !== "number") {
			return bad("the timeout is not a number");
		}
		const command = args.command.replace(/\s+/g, " ").trim();
		return { tool, command, action: `command:${command}`, summary: summarise(command) };
	}

	return bad(`this assistant does not know how to judge a tool called ${tool}`);
}

/**
 * A short, redacted form of a command, for a log line or a spoken question.
 * Whatever follows a token, password or key is replaced: the log has to be
 * useful for diagnosis without becoming the place secrets end up.
 */
export function summarise(text: string): string {
	const redacted = text
		.replace(/(bearer\s+)[^\s'"]+/gi, "$1[redacted]")
		.replace(/((?:api[_-]?key|token|secret|password|passwd|authorization)(?:\s*[=:]\s*|\s+))[^\s'"]+/gi,
			"$1[redacted]");
	return redacted.length > MAX_SUMMARY ? `${redacted.slice(0, MAX_SUMMARY)}...` : redacted;
}

// ------------------------------------------------------------- what is allowed

function splitSegments(command: string): string[] {
	return command.split(/[|;&]+/).map((part) => part.trim()).filter(Boolean);
}

function tokenise(segment: string): string[] {
	return segment.split(/\s+/);
}

function baseName(word: string): string {
	const cut = word.lastIndexOf("/");
	return (cut === -1 ? word : word.slice(cut + 1)).toLowerCase();
}

/** Whether a command only looks. Conservative on purpose: anything unclear is not. */
export function readonlyCommand(command: string, extra: string[]): { ok: boolean; why: string } {
	if (/[><`]|\$\(|\$\{/.test(command)) {
		return { ok: false, why: "redirects or builds a command from something else" };
	}
	const allowed = new Set([...READONLY_COMMANDS, ...extra.map((word) => word.toLowerCase())]);
	const words = command.split(/\s+/).map((word) => word.replace(/^['"]|['"]$/g, "")).filter(Boolean);
	for (const word of words) {
		const name = baseName(word).replace(/[;|&]/g, "");
		if (NEVER_READONLY.has(name)) {
			return { ok: false, why: `uses ${name}, which changes the machine` };
		}
	}
	for (const segment of splitSegments(command)) {
		const parts = tokenise(segment);
		const name = baseName(parts[0] ?? "");
		if (!allowed.has(name)) {
			return { ok: false, why: `runs ${name || "nothing"}, which is not on the read-only list` };
		}
		const subcommands = SUBCOMMANDS[name];
		if (subcommands) {
			const second = (parts[1] ?? "").toLowerCase();
			if (!subcommands.has(second) && !subcommands.has(parts[1] ?? "")) {
				return { ok: false, why: `${name} ${second} is not one of the read-only ones` };
			}
		}
		if (name === "niri") {
			// flags may come before the subcommand: `niri msg -j windows` is how the
			// window list is asked for, and it is the same read as `niri msg windows`.
			const rest = parts.slice(1).filter((word) => !word.startsWith("-"));
			if (rest.length) {
				if (rest[0] !== "msg") {
					return { ok: false, why: `niri ${rest[0]} is not one of the read-only ones` };
				}
				if (rest[1] && !NIRI_MSG_READS.has(rest[1])) {
					return { ok: false, why: `niri msg ${rest[1]} changes the machine` };
				}
			}
		}
		if (PASS_THROUGH.has(name)) {
			// A variable assignment or a flag is fine; a program name is another
			// program, and this list cannot vouch for a name it has never seen.
			// `command -v` and `command -V` are the exception: their argument is a
			// name to look up, not a program to run, and they only print.
			let looksUp = false;
			for (const part of parts.slice(1)) {
				if (part === "-v" || part === "-V") {
					looksUp = true;
					continue;
				}
				if (part.startsWith("-") || /^[A-Za-z_][A-Za-z0-9_]*=/.test(part)) continue;
				if (looksUp) continue;
				return { ok: false, why: `${name} would run ${part}` };
			}
		}
		const flags = [...WRITING_FLAGS, ...(COMMAND_FLAGS[name] ?? [])];
		for (const part of parts.slice(1)) {
			if (flags.some((pattern) => pattern.test(part))) {
				return { ok: false, why: `uses ${part}, which writes` };
			}
		}
	}
	return { ok: true, why: "it only looks" };
}

export function actionDigest(tool: string, input: unknown): string {
	const cwd = process.cwd();
	const call = canonicalise(tool, input, cwd);
	const action = "invalid" in call ? `invalid:${tool}` : call.action;
	return createHash("sha256").update(`${tool}\n${action}`).digest("hex");
}

// ------------------------------------------------------------------ approvals

interface Token {
	digest?: string;
	granted?: number;
	id?: string;
	by?: string;
	used?: number;
}

function readTokens(policy: Policy): Token[] | null {
	if (!policy.approvals) return null;
	if (!existsSync(policy.approvals)) return [];
	try {
		const parsed = JSON.parse(readFileSync(policy.approvals, "utf8"));
		const tokens = (parsed as { tokens?: unknown }).tokens;
		if (!Array.isArray(tokens)) return null;
		return tokens as Token[];
	} catch {
		return null;
	}
}

function writeJson(file: string, body: unknown): boolean {
	try {
		mkdirSync(dirname(file), { recursive: true });
		const temporary = `${file}.${process.pid}.tmp`;
		writeFileSync(temporary, JSON.stringify(body, null, 2), { mode: 0o600 });
		renameSync(temporary, file);
		return true;
	} catch {
		return false;
	}
}

/**
 * Merge the tokens we just changed with whatever is on disk now.
 *
 * Two writers share this file and neither can lock it: ARCHY writes an approval
 * when the owner says so, and this file burns one the moment it is used. A plain
 * "write back what I read" loses whichever of those happened in the microseconds
 * in between, and losing a burn is the worse half: the same approved action runs
 * again. So the write-back re-reads, keeps every token for a digest it did not
 * touch, and keeps our own verdict for the ones it did. Pure, so the rule can be
 * tested without staging a race.
 */
export function mergeTokens(onDisk: Token[], ours: Token[]): Token[] {
	const mine = new Set(ours.map((token) => token.digest));
	const others = onDisk.filter((token) => !mine.has(token.digest));
	return [...others, ...ours];
}

/**
 * Present an approval. Used once, and the use is written down, so a retried turn
 * or a resend after a dropped connection cannot run the same destructive action
 * twice.
 */
export function consumeApproval(digest: string, policy: Policy | Unusable): string {
	if (isUnusable(policy)) return "unusable";
	if (!policy.enabled) return "off";
	const tokens = readTokens(policy as Policy);
	if (tokens === null) return "unusable";
	const ttl = typeof (policy as Policy).approval_ttl_s === "number"
		? (policy as Policy).approval_ttl_s! : 300;
	const now = Date.now() / 1000;
	// A live token wins over a spent one. Both can be in the file: ARCHY keeps
	// spent tokens so a burn cannot be forgotten, which means a second, deliberate
	// approval of the same action has to be found by looking past the first one.
	let burned = false;
	for (const token of tokens) {
		if (token.digest !== digest) continue;
		if (token.used) { burned = true; continue; }
		if (typeof token.granted !== "number" || now - token.granted > ttl) {
			return "expired";
		}
		token.used = now;
		const current = readTokens(policy as Policy) ?? [];
		if (!writeJson((policy as Policy).approvals!, { tokens: mergeTokens(current, tokens) })) {
			return "unusable";
		}
		return "granted";
	}
	return burned ? "burned" : "none";
}

interface Pending {
	id?: string;
	digest?: string;
	tool?: string;
	summary?: string;
	asked?: number;
	capability?: string;
}

/** Write down what was blocked, so the owner can be asked outside the turn. */
function notePending(policy: Policy, tool: string, summary: string, digest: string): void {
	if (!policy.pending) return;
	let entries: Pending[] = [];
	if (existsSync(policy.pending)) {
		try {
			const parsed = JSON.parse(readFileSync(policy.pending, "utf8"));
			if (Array.isArray((parsed as { pending?: unknown }).pending)) {
				entries = (parsed as { pending: Pending[] }).pending;
			}
		} catch {
			entries = [];                    // an unreadable list is replaced, not trusted
		}
	}
	const keep = entries.filter((entry) => entry.digest !== digest).slice(-(MAX_PENDING - 1));
	keep.push({ id: digest.slice(0, 8), digest, tool, summary, asked: Date.now() / 1000 });
	writeJson(policy.pending, { pending: keep });
}

// -------------------------------------------------------------------- the log

function logDecision(policy: Policy, tool: string, decision: string, reason: string,
	summary: string): void {
	if (!policy.log) return;
	const line = JSON.stringify({
		ts: new Date().toISOString(), tool, decision, reason,
		summary: summarise(String(summary)), pid: process.pid,
	});
	try {
		mkdirSync(dirname(policy.log), { recursive: true });
		writeFileSync(policy.log, `${line}\n`, { flag: "a", mode: 0o600 });
	} catch {
		// A log that cannot be written is not a reason to allow anything, and it
		// is not a reason to block an approved action either.
	}
}

// ---------------------------------------------------------------- the decision

export function decide(toolName: string, input: unknown, loaded: Loaded, cwd: string): Decision {
	if (isUnusable(loaded)) {
		logDecision({ log: undefined }, toolName, "deny", loaded.unusable, "");
		return { allow: false, reason: `I cannot tell what I am allowed to do: ${loaded.unusable}.`,
			approval: false, enforcing: true };
	}
	const policy = loaded as Policy;
	if (policy.enabled === false) {
		return { allow: true, enforcing: false };
	}
	const roots = policy.roots as string[];
	const extra = Array.isArray(policy.readonly_commands)
		? (policy.readonly_commands as string[]).filter(isString) : [];

	// A privacy setting is not something an approval can override. An approval says
	// "do this one thing"; this says "this kind of thing does not go to that model",
	// and a question about it would be an offer to make a promise the owner already
	// declined. So it refuses, without offering one, and says which setting it was.
	if (policy.privacy_remote === true && policy.privacy_allow_files === false
		&& FILE_TOOLS.has(toolName)) {
		const reason = "sending file contents to an online model is switched off, so I "
			+ "will not read or write files while one is answering";
		logDecision(policy, toolName, "deny", reason, "");
		return { allow: false, reason: `I will not do that: ${reason}.`, approval: false, enforcing: true };
	}

	const call = canonicalise(toolName, input, cwd);
	if ("invalid" in call) {
		const reason = `I will not run that: ${call.invalid}.`;
		logDecision(policy, toolName, "deny", reason, "");
		return { allow: false, reason, approval: false, enforcing: true };
	}
	const digest = createHash("sha256").update(`${toolName}\n${call.action}`).digest("hex");

	if (call.command !== undefined) {
		const verdict = readonlyCommand(call.command, extra);
		if (verdict.ok) {
			logDecision(policy, toolName, "allow", verdict.why, call.summary);
			return { allow: true, digest, enforcing: true };
		}
		return refuse(policy, toolName, call, digest,
			`that command ${verdict.why}, so it needs your approval`);
	}

	if (insideRoots(call.path!, roots)) {
		logDecision(policy, toolName, "allow", "inside an approved directory", call.summary);
		return { allow: true, digest, enforcing: true };
	}
	return refuse(policy, toolName, call, digest,
		`${call.path} is outside the directories I am allowed to touch`);
}

function refuse(policy: Policy, tool: string, call: Call, digest: string, why: string): Decision {
	if (!policy.approvals) {
		// No file to write an approval into, so there is no way to ask and no way
		// to remember the answer. Refusing without offering an approval is the
		// honest end of that, rather than a question nobody can answer.
		const reason = "there is nowhere to record an approval, so I will not do this one";
		logDecision(policy, tool, "deny", reason, call.summary);
		return { allow: false, reason: `I will not run that: ${reason}.`, approval: false, enforcing: true };
	}
	const token = consumeApproval(digest, policy);
	if (token === "granted") {
		logDecision(policy, tool, "granted", "approved by the owner for this one action", call.summary);
		return { allow: true, digest, approved: true, enforcing: true };
	}
	if (token === "burned") {
		const reason = "that exact action was approved already and has already run, so I will not run it again";
		logDecision(policy, tool, "burned", reason, call.summary);
		return { allow: false, reason: `I will not run that again: ${reason}.`, approval: false, enforcing: true };
	}
	if (token === "unusable") {
		const reason = "the record of your approvals cannot be read, so I will not act on anything";
		logDecision(policy, tool, "deny", reason, call.summary);
		return { allow: false, reason: `I will not run that: ${reason}.`, approval: false, enforcing: true };
	}
	logDecision(policy, tool, "deny", why, call.summary);
	notePending(policy, tool, call.summary, digest);
	return { allow: false, digest, approval: true, enforcing: true,
		reason: `I need your approval first: ${why}.` };
}

// ------------------------------------------------------------- the extension

export default function permissionGate(pi: ExtensionAPI) {
	pi.on("tool_call", async (event) => {
		try {
			const policy = loadPolicy();
			const decision = decide(event.toolName, event.input, policy, process.cwd());
			if (decision.allow) return undefined;
			return { block: true, reason: decision.reason };
		} catch (error) {
			// Never an accidental allow: pi blocks a thrown handler anyway, this
			// is so the block comes with a reason someone can act on.
			return { block: true,
				reason: `the permission gate failed (${(error as Error).message}), so I did not run that` };
		}
	});
}
