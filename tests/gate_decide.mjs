// The bridge between the Python tests and the real permission gate.
//
// The gate is a pi extension written in TypeScript, because it runs inside pi and
// pi loads extensions with jiti. Testing it from the Python suite would mean
// testing a copy of the policy kept in step by hand, which is how a gate ends up
// disagreeing with its tests. Node strips the types and runs the actual file.
//
// One line in, one line out, in the order they arrive, so the caller can pipe a
// batch and match answers to requests.
import { createInterface } from "node:readline";

import { actionDigest, consumeApproval, decide, loadPolicy } from "../src/jarvis/gate/permission-gate.ts";

const lines = createInterface({ input: process.stdin });

lines.on("line", (line) => {
	if (!line.trim()) return;
	let answer;
	try {
		const request = JSON.parse(line);
		// A path exercises reading the policy off disk, which is how it runs for
		// real; an inline object keeps the pure decisions readable.
		const policy = request.policyPath ? loadPolicy(request.policyPath) : request.policy;
		if (request.mode === "digest") {
			answer = { digest: actionDigest(request.toolName, request.input) };
		} else if (request.mode === "consume") {
			// The caller may name the digest or the action it belongs to; asking for
			// an approval is always about an action, so deriving it here keeps both
			// spellings the same hash.
			const digest = request.digest ?? actionDigest(request.toolName, request.input);
			answer = { digest, consumed: consumeApproval(digest, policy) };
		} else {
			answer = decide(request.toolName, request.input, policy, request.cwd ?? process.cwd());
		}
	} catch (error) {
		answer = { error: String((error && error.message) || error) };
	}
	process.stdout.write(`${JSON.stringify(answer)}\n`);
});
