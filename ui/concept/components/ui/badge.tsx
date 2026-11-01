// shadcn/ui Badge subset (MIT). Research status, never an investment certification.
import type { ComponentProps } from "react";
import { cn } from "../../lib/utils";

export function Badge({ className, ...props }: ComponentProps<"span">) {
	return (
		<span
			data-slot="badge"
			className={cn("research-badge", className)}
			{...props}
		/>
	);
}
