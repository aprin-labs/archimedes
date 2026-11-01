import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { MotionConfig } from "motion/react";
import App from "./App.tsx";
import "./styles.css";

const root = document.getElementById("root");
if (!root) throw new Error("Preview root element is missing.");

createRoot(root).render(
	<StrictMode>
		<MotionConfig reducedMotion="user" transition={{ duration: 0.22, ease: [0.22, 1, 0.36, 1] }}>
			<App />
		</MotionConfig>
	</StrictMode>,
);
