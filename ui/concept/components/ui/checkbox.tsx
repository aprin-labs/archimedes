// shadcn/ui Checkbox (MIT). Radix handles checked/indeterminate and keyboard state.
import type { ComponentProps } from "react";
import { Checkbox as Primitive } from "radix-ui";
import { cn } from "../../lib/utils";
import { Icon } from "../../UI";

export function Checkbox({
	className,
	...props
}: ComponentProps<typeof Primitive.Root>) {
	return (
		<Primitive.Root
			data-slot="checkbox"
			className={cn("research-checkbox", className)}
			{...props}
		>
			<span className="checkbox-box">
				<Primitive.Indicator>
					<Icon name="check" />
				</Primitive.Indicator>
			</span>
		</Primitive.Root>
	);
}
