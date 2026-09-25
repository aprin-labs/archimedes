import { useState, useEffect, useCallback } from "react";
import { apiPost, apiGet } from "../api";
import { getAddress, usdcBalanceOfRaw, MIN_VAULT_FUNDS_RAW } from "../config";
import StrategyTable from "./PublishStrategyTable";
import CreateVaultModal from "./CreateVaultModal";

const USDC_DECIMALS = 6;

function formatUsdc(raw) {
	return (Number(raw) / 10 ** USDC_DECIMALS).toLocaleString(undefined, {
		minimumFractionDigits: 2,
		maximumFractionDigits: 2,
	});
}

export default function PublishPage({ onNavigate }) {
	const walletAddr = getAddress();
	const [activeTab, setActiveTab] = useState("generated");
	const [generated, setGenerated] = useState([]);
	const [examples, setExamples] = useState([]);
	const [loading, setLoading] = useState(false);
	const [loadError, setLoadError] = useState("");
	const [selectedStrategy, setSelectedStrategy] = useState(null); // { id, title }

	// Publish menu state
	const [vaultMode, setVaultMode] = useState("create"); // 'create' | 'existing'
	const [existingVault, setExistingVault] = useState("");
	const [publishing, setPublishing] = useState(false);
	const [publishError, setPublishError] = useState("");

	// Vault creation / deposit flow
	const [showCreateModal, setShowCreateModal] = useState(false);
	const [createdVault, setCreatedVault] = useState(null); // address after create+deposit
	const [depositDone, setDepositDone] = useState(false); // true once DepositFlow completes
	const [vaultBalance, setVaultBalance] = useState(null); // bigint raw
	const [checkingBalance, setCheckingBalance] = useState(false);

	// Result panel
	const [result, setResult] = useState(null);

	const load = useCallback(async () => {
		setLoading(true);
		setLoadError("");
		try {
			const [genRes, exRes] = await Promise.all([
				apiGet("/api/strategies/generated"),
				apiGet("/api/strategies/"),
			]);
			setGenerated(genRes.strategies || []);
			setExamples(exRes.strategies || []);
		} catch (err) {
			setLoadError(err.message || "Strategies could not load");
		} finally {
			setLoading(false);
		}
	}, []);

	useEffect(() => {
		load();
	}, [load]);

	// Check vault balance whenever vault address changes
	useEffect(() => {
		// Early-return paths must reset checkingBalance too: when deps change
		// mid-flight, the cleanup cancels the old run (its guarded .finally never
		// fires) — if the new run then bails here, the UI would stay stuck on
		// "Checking balance…" forever (review).
		if (!vaultMode || !selectedStrategy) {
			setCheckingBalance(false);
			return;
		}
		const addr = vaultMode === "create" ? createdVault : existingVault.trim();
		if (!addr) {
			setVaultBalance(null);
			setCheckingBalance(false);
			return;
		}
		let cancelled = false;
		setCheckingBalance(true);
		usdcBalanceOfRaw(addr)
			.then((b) => {
				if (!cancelled) setVaultBalance(b);
			})
			.catch(() => {
				if (!cancelled) setVaultBalance(null);
			})
			.finally(() => {
				if (!cancelled) setCheckingBalance(false);
			});
		return () => {
			cancelled = true;
		};
	}, [vaultMode, createdVault, existingVault, selectedStrategy, depositDone]);

	const resetMenu = () => {
		setSelectedStrategy(null);
		setVaultMode("create");
		setExistingVault("");
		setCreatedVault(null);
		setDepositDone(false);
		setVaultBalance(null);
		setPublishing(false);
		setPublishError("");
		setShowCreateModal(false);
	};

	const balanceOk =
		vaultBalance !== null && vaultBalance >= MIN_VAULT_FUNDS_RAW;
	const vaultAddress =
		vaultMode === "create" ? createdVault : existingVault.trim();

	const handlePublish = async () => {
		if (!selectedStrategy || !vaultAddress) return;
		setPublishing(true);
		setPublishError("");
		try {
			const res = await apiPost("/api/marketplace/publish", {
				strategy_id: selectedStrategy.id,
				vault_address: vaultAddress,
			});
			setResult(res);
		} catch (err) {
			setPublishError(err.message || "Publish failed");
		} finally {
			setPublishing(false);
		}
	};

	// ── Callbacks for CreateVaultModal ──
	// onDeployed fires right after on-chain createVault+setAgent succeed (before DepositFlow).
	// We capture the address early so it's ready when DepositFlow completes.
	const handleVaultDeployed = (addr) => {
		setCreatedVault(addr);
	};
	// onClose fires when the modal fully closes (after DepositFlow's onComplete or user cancel).
	// If we captured a vault address, assume the deposit flow completed.
	const handleCloseModal = () => {
		if (createdVault) setDepositDone(true);
		setShowCreateModal(false);
	};

	// ── Not connected ──
	if (!walletAddr) {
		return (
			<div className="page-panel">
				<div className="max-w-[540px] mx-auto">
					<h1 className="text-[2rem] font-semibold mb-2">Publish a strategy</h1>
					<p className="body text-[var(--color-muted)]">
						Connect your wallet to publish a strategy to the marketplace. Your
						strategy will be visible to all users who can subscribe and
						copy-trade.
					</p>
				</div>
			</div>
		);
	}

	// ── Success result panel ──
	if (result) {
		return (
			<div className="page-panel">
				<div className="max-w-[540px] mx-auto">
					<h1 className="text-[2rem] font-semibold mb-2">Strategy published</h1>
					<div className="card p-4 space-y-2 text-sm">
						<div>
							<span className="text-[var(--color-muted)]">Strategy:</span>{" "}
							{result.strategy_id}
						</div>
						<div>
							<span className="text-[var(--color-muted)]">Pool ID:</span>{" "}
							<span className="font-mono">{result.pool_id}</span>
						</div>
						<div>
							<span className="text-[var(--color-muted)]">Vault:</span>{" "}
							<span className="font-mono">{result.vault_address}</span>
						</div>
						<div>
							<span className="text-[var(--color-muted)]">Status:</span>{" "}
							{result.status}
						</div>
					</div>
					<div className="flex gap-2 mt-4">
						<button className="btn" onClick={() => onNavigate("marketplace")}>
							View Marketplace
						</button>
						<button
							className="btn btn-ghost"
							onClick={() => {
								setResult(null);
								resetMenu();
							}}
						>
							Publish Another
						</button>
					</div>
				</div>
			</div>
		);
	}

	// ── Inline publish menu for a selected strategy ──
	if (selectedStrategy) {
		return (
			<div className="page-panel">
				<div className="max-w-[640px] mx-auto">
					<button className="btn btn-ghost btn-sm mb-4" onClick={resetMenu}>
						← Back to strategies
					</button>

					<h1 className="text-[1.8rem] font-semibold mb-1">Publish</h1>
					<p className="body text-[var(--color-muted)] mb-4">
						Strategy:{" "}
						<strong>{selectedStrategy.title || selectedStrategy.id}</strong> (
						{selectedStrategy.id})
					</p>

					{/* Vault choice */}
					<div className="card p-4 space-y-4">
						<div>
							<label className="caption block mb-2">Vault</label>
							<div className="flex gap-3 mb-2">
								<button
									className={`btn btn-sm ${vaultMode === "create" ? "btn-primary" : "btn-outline"}`}
									onClick={() => setVaultMode("create")}
								>
									Create new vault
								</button>
								<button
									className={`btn btn-sm ${vaultMode === "existing" ? "btn-primary" : "btn-outline"}`}
									onClick={() => setVaultMode("existing")}
								>
									Use existing vault
								</button>
							</div>
						</div>

						{vaultMode === "create" && (
							<div>
								{!createdVault ? (
									<button
										className="btn btn-primary"
										onClick={() => setShowCreateModal(true)}
									>
										Create Vault + Deposit
									</button>
								) : (
									<div className="space-y-2">
										<div className="text-sm">
											<span className="text-[var(--color-muted)]">Vault:</span>{" "}
											<span className="font-mono">{createdVault}</span>
										</div>
										{checkingBalance && (
											<div className="caption">Checking balance…</div>
										)}
										{!checkingBalance && vaultBalance !== null && (
											<div
												className={`text-sm ${balanceOk ? "text-[var(--positive)]" : "text-[var(--color-danger)]"}`}
											>
												USDC balance: {formatUsdc(vaultBalance)}{" "}
												{!balanceOk &&
													`(minimum required: ${formatUsdc(MIN_VAULT_FUNDS_RAW)})`}
											</div>
										)}
										<button
											className="btn btn-sm btn-ghost"
											onClick={() => setShowCreateModal(true)}
										>
											Re-create vault
										</button>
									</div>
								)}
							</div>
						)}

						{vaultMode === "existing" && (
							<div className="space-y-2">
								<label htmlFor="existing-vault" className="caption block mb-1">
									Existing vault address
								</label>
								<input
									id="existing-vault"
									name="existing_vault"
									type="text"
									autoComplete="off"
									spellCheck="false"
									className="input w-full font-mono text-sm"
									value={existingVault}
									onChange={(e) => setExistingVault(e.target.value)}
									placeholder="0x..."
								/>
								{checkingBalance && (
									<div className="caption">Checking balance…</div>
								)}
								{!checkingBalance &&
									vaultBalance !== null &&
									existingVault.trim() && (
										<div
											className={`text-sm ${balanceOk ? "text-[var(--positive)]" : "text-[var(--color-danger)]"}`}
										>
											USDC balance: {formatUsdc(vaultBalance)}{" "}
											{!balanceOk &&
												`(minimum required: ${formatUsdc(MIN_VAULT_FUNDS_RAW)})`}
										</div>
									)}
							</div>
						)}

						{/* Publish button */}
						<div className="pt-2">
							{publishError && (
								<p
									className="text-sm text-[var(--color-danger)] mb-2"
									role="alert"
								>
									{publishError}
								</p>
							)}
							<button
								className="btn btn-primary"
								disabled={!vaultAddress || !balanceOk || publishing}
								onClick={handlePublish}
							>
								{publishing ? "Publishing…" : "Publish"}
							</button>
							{!balanceOk && vaultAddress && !checkingBalance && (
								<p className="caption text-[var(--color-danger)] mt-1">
									Vault holds insufficient USDC. Publishing requires ≥{" "}
									{formatUsdc(MIN_VAULT_FUNDS_RAW)} USDC.
								</p>
							)}
						</div>
					</div>
				</div>

				{/* CreateVaultModal portal — handles createVault + setAgent + DepositFlow */}
				{showCreateModal && (
					<CreateVaultModal
						strategy={{
							id: selectedStrategy.id,
							paper_title: selectedStrategy.title || selectedStrategy.id,
						}}
						walletAddr={walletAddr}
						onClose={handleCloseModal}
						onDeployed={handleVaultDeployed}
					/>
				)}
			</div>
		);
	}

	// ── Main tabbed view ──
	return (
		<div className="page-panel">
			<div className="max-w-[960px] mx-auto">
				<header className="app-page-heading">
					<p className="app-eyebrow">Creator settlement</p>
					<h1>Publish a strategy</h1>
					<p>
						Select a strategy to publish to the on-chain marketplace. You need a
						vault with at least {formatUsdc(MIN_VAULT_FUNDS_RAW)} USDC to
						publish.
					</p>
				</header>

				<div className="flex gap-2 mb-4">
					<button
						className={`tag tag-tab ${activeTab === "generated" ? "tag-accent" : "tag-muted"}`}
						onClick={() => setActiveTab("generated")}
					>
						Generated
					</button>
					<button
						className={`tag tag-tab ${activeTab === "examples" ? "tag-accent" : "tag-muted"}`}
						onClick={() => setActiveTab("examples")}
					>
						Examples
					</button>
				</div>

				{loading && (
					<div className="caption" aria-live="polite">
						Loading…
					</div>
				)}
				{loadError && (
					<div className="status mb-4" role="alert">
						{loadError}
					</div>
				)}

				{!loading && !loadError && activeTab === "generated" && (
					<StrategyTable
						strategies={generated}
						onSelect={(s) => setSelectedStrategy(s)}
					/>
				)}

				{!loading && !loadError && activeTab === "examples" && (
					<StrategyTable
						strategies={examples}
						onSelect={(s) => setSelectedStrategy(s)}
					/>
				)}
			</div>
		</div>
	);
}
