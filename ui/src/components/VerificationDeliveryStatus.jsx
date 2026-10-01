import { deriveVerificationDeliveryView } from "../verificationDelivery";

// The one place verification-mail delivery state is rendered (#1748 item 2).
//
// Two mount points — Account Settings and the Generate page's resend control
// (ResendVerificationControl) — share this component for the same reason they
// already share VERIFICATION_REQUESTED_MESSAGE: two surfaces answering "did my
// verification email arrive?" differently is how one of them ends up lying.
//
// Every string comes from verificationDelivery.js; nothing is composed here.
// `blocked` (suppressed) is an alert because it is the one state the user must
// act on — resending cannot work — and the rest are status.
export default function VerificationDeliveryStatus({ status }) {
	const view = deriveVerificationDeliveryView(status);
	if (!view) return null;

	return (
		<div
			className="status"
			data-delivery-state={view.state}
			role={view.tone === "blocked" || view.tone === "error" ? "alert" : "status"}
		>
			{view.message}
		</div>
	);
}
