import { useCallback, useEffect, useState } from "react";
import { getVerificationStatus, resendVerificationEmail } from "../auth-client";
import { VERIFICATION_REQUESTED_MESSAGE } from "../freeGenerations";
import {
	deriveVerificationDeliveryView,
	RATE_LIMITED_BY_CLIENT,
	shouldShowRequestedFallback,
} from "../verificationDelivery";
import VerificationDeliveryStatus from "./VerificationDeliveryStatus";

// On-demand verification resend for the Generate page (locked free-tier
// banner + the 409 wallet-gate panel). Account Settings already has this
// button; #1658 deferred the Generate-page copy to the #1642 redesign,
// which landed without it. One control, two mount points, so the two
// surfaces cannot drift on honesty (queued ≠ delivered).
//
// #1748 item 2: the button's own `{status:true}` says nothing, so the control
// also reads GET /api/auth/verification-status — the auth service's record of
// what happened to this address's mail — and renders it through the same
// component Account Settings uses. Without it, a user whose address SES has
// suppressed sits on this page pressing a button that can never work.
//
// Renders nothing when there is no email to send to — a button that POSTs
// an empty address would be a claim this control cannot keep.
export default function ResendVerificationControl({ email }) {
	const [status, setStatus] = useState("idle");
	const [error, setError] = useState("");
	const [delivery, setDelivery] = useState(null);

	const refreshDelivery = useCallback(async () => {
		try {
			setDelivery(await getVerificationStatus());
		} catch {
			// 401 / 503 / offline — none of which is knowledge about this
			// address's mail, so nothing is shown rather than a hopeful default.
			setDelivery(null);
		}
	}, []);

	useEffect(() => {
		if (email) refreshDelivery();
	}, [email, refreshDelivery]);

	const send = async () => {
		setStatus("sending");
		setError("");
		try {
			await resendVerificationEmail(email, `${window.location.origin}/app`);
			setStatus("sent");
			await refreshDelivery();
		} catch (err) {
			setError(err?.message || "Could not request a verification email.");
			setStatus("error");
			// Better Auth's limiter keys on client IP, not address, so a 429 is
			// something only this client saw.
			if (err?.status === 429) setDelivery(RATE_LIMITED_BY_CLIENT);
			else await refreshDelivery();
		}
	};

	// Hooks must run before any early return, so the no-email case bails here.
	if (!email) return null;

	const blocked = deriveVerificationDeliveryView(delivery)?.canResend === false;

	return (
		<div className="generate-resend" style={{ marginTop: 8 }}>
			<button
				type="button"
				className="btn btn-outline btn-sm"
				onClick={send}
				disabled={status === "sending" || blocked}
			>
				{status === "sending" ? "Sending…" : "Resend verification email"}
			</button>
			{/* Pre-status fallback only — see shouldShowRequestedFallback. Once
			    the delivery panel has a recognised state for this click it owns
			    the answer, so the eternal "requested" line stands down rather
			    than contradicting it. */}
			{status === "sent" && shouldShowRequestedFallback(delivery) && (
				<p className="caption mb-0" role="status" style={{ marginTop: 6 }}>
					{VERIFICATION_REQUESTED_MESSAGE}
				</p>
			)}
			{status === "error" && (
				<p className="caption mb-0" role="alert" style={{ marginTop: 6 }}>
					{error}
				</p>
			)}
			<VerificationDeliveryStatus status={delivery} />
		</div>
	);
}
