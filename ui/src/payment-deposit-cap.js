// FE-01 (#1892): the device payment key sits in cleartext localStorage (see
// payment-session.js), so whatever gets deposited into it is its whole blast
// radius. Only the passkey path funds that key; EOA wallets deposit from
// their own wallet and aren't capped here.

export const MAX_SESSION_KEY_DEPOSIT_RAW = 50_000_000n; // 50.00 USDC (6 decimals)
export const MAX_SESSION_KEY_DEPOSIT_USD = "50";

/** Error message for a device-payment-key deposit, or null if it's allowed. */
export function sessionKeyDepositError(amountRaw, need) {
	if (amountRaw < need) return "Deposit amount must at least cover the generation price.";
	if (amountRaw > MAX_SESSION_KEY_DEPOSIT_RAW) {
		return (
			`Deposits to the device payment key are capped at $${MAX_SESSION_KEY_DEPOSIT_USD}. ` +
			"The key is stored unencrypted in this browser, so the cap limits what's at risk."
		);
	}
	return null;
}
