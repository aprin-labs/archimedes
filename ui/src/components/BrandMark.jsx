export default function BrandMark({ className = "" }) {
	return (
		<span
			className={`brand-mark${className ? ` ${className}` : ""}`}
			aria-hidden="true"
		/>
	);
}
