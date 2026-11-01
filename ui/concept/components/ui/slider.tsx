// Adapted from shadcn/ui (MIT). Single-value research-budget control, not a range editor.
import type { ComponentProps } from "react";
import { Slider as Primitive } from "radix-ui";
import { cn } from "../../lib/utils";

type Props = ComponentProps<typeof Primitive.Root> & {
	label: string;
	valueText: string;
};
export function Slider({ className, label, valueText, ...props }: Props) {
	return (
		<Primitive.Root
			data-slot="slider"
			className={cn(
				"relative flex min-h-11 w-full touch-none select-none items-center",
				className,
			)}
			{...props}
		>
			<Primitive.Track className="relative h-1 w-full grow overflow-hidden rounded-full bg-input">
				<Primitive.Range className="absolute h-full bg-control" />
			</Primitive.Track>
			<Primitive.Thumb
				aria-label={label}
				aria-valuetext={valueText}
				className="block size-6 rounded-full border-2 border-control bg-background transition-shadow hover:ring-4 hover:ring-control/15 focus-visible:outline-3 focus-visible:outline-offset-4 focus-visible:outline-ring"
			/>
		</Primitive.Root>
	);
}
