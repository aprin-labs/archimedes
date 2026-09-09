// Kibo Table presentation + column-header adaptation (MIT, shadcnblocks).
// Reuses Observatory's local sorting/filter/selection state. No TanStack/Jotai
// provider: the existing small in-memory collection already supplies that behavior.
import type { ComponentProps } from "react";
import { Button } from "../ui/button";
import { Icon } from "../../UI";
import { cn } from "../../lib/utils";

export function Table({ className, ...props }: ComponentProps<"table">) {
	return (
		<table
			data-slot="table"
			className={cn("strategy-table", className)}
			{...props}
		/>
	);
}
export function TableColumnHeader({
	title,
	direction,
	onSort,
}: {
	title: string;
	direction?: "asc" | "desc";
	onSort: () => void;
}) {
	return (
		<Button
			variant="ghost"
			size="sm"
			className="column-sort"
			aria-label={`Sort by ${title}`}
			onClick={onSort}
		>
			<span>{title}</span>
			<Icon
				name={direction ? "arrow-down" : "sliders-horizontal"}
				className={direction === "asc" ? "rotate-180" : ""}
			/>
		</Button>
	);
}
