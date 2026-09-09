// Adapted from Kibo Choicebox (MIT, shadcnblocks). Same radio/title/description
// composition, flattened to one item button instead of a context per option.
import { useId, type ComponentProps } from "react";
import { RadioGroup } from "radix-ui";
import { cn } from "../../lib/utils";

export function Choicebox({
	className,
	...props
}: ComponentProps<typeof RadioGroup.Root>) {
	return (
		<RadioGroup.Root
			data-slot="choicebox"
			className={cn("choicebox", className)}
			{...props}
		/>
	);
}
export function ChoiceboxItem({
	title,
	description,
	className,
	...props
}: ComponentProps<typeof RadioGroup.Item> & {
	title: string;
	description: string;
}) {
	const id = useId();
	return (
		<RadioGroup.Item
			data-slot="choicebox-item"
			aria-labelledby={`${id}-title`}
			aria-describedby={`${id}-description`}
			className={cn("choicebox-item", className)}
			{...props}
		>
			<span className="choicebox-indicator">
				<RadioGroup.Indicator />
			</span>
			<span className="choicebox-content">
				<span id={`${id}-title`} className="choicebox-title">
					{title}
				</span>
				<span id={`${id}-description`} className="choicebox-description">
					{description}
				</span>
			</span>
			<span className="choicebox-selected" aria-hidden="true">
				Selected
			</span>
		</RadioGroup.Item>
	);
}
