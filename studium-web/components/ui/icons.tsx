/**
 * The icon vocabulary (spec §13.3, WCAG 3.2.4; §16.6).
 *
 * §13.3 requires that the same icon means the same thing everywhere, and names
 * this file as where those meanings are documented. One export per *meaning*,
 * not per glyph -- importing `Hand` directly from lucide is what lets a second
 * component use it for something that is not interruption.
 */
import {
  AlertTriangle,
  ArrowRight,
  BookOpen,
  Check,
  ChevronDown,
  CircleHelp,
  Clock,
  Command,
  Hand,
  Loader2,
  MinusCircle,
  NotebookPen,
  Search,
  Settings,
  Sigma,
  WifiOff,
  X,
  XCircle,
  type LucideIcon,
} from "lucide-react";

/** §16.6: 20px default, 16px in dense contexts, 1.5px stroke. */
export const ICON_SIZE = 20;
export const ICON_SIZE_DENSE = 16;
export const ICON_STROKE = 1.5;

/** Interruption. Never used for anything else (§13.3). */
export const InterruptIcon: LucideIcon = Hand;

/** Verdicts. Each pairs with text -- never colour alone (§13.1, WCAG 1.4.1). */
export const CorrectIcon: LucideIcon = Check;
export const PartialIcon: LucideIcon = MinusCircle;
export const IncorrectIcon: LucideIcon = XCircle;

export const WarningIcon: LucideIcon = AlertTriangle;
export const OfflineIcon: LucideIcon = WifiOff;
export const PendingIcon: LucideIcon = Loader2;

export const PaletteIcon: LucideIcon = Command;
export const SearchIcon: LucideIcon = Search;
export const HelpIcon: LucideIcon = CircleHelp;
export const CloseIcon: LucideIcon = X;
export const ExpandIcon: LucideIcon = ChevronDown;
export const ContinueIcon: LucideIcon = ArrowRight;

export const LectureIcon: LucideIcon = BookOpen;
export const JournalIcon: LucideIcon = NotebookPen;
export const MasteryIcon: LucideIcon = Sigma;
export const TimerIcon: LucideIcon = Clock;
export const SettingsIcon: LucideIcon = Settings;

/** The default props every icon in the app renders with. */
export const iconProps = {
  size: ICON_SIZE,
  strokeWidth: ICON_STROKE,
  "aria-hidden": true,
} as const;

export const denseIconProps = {
  size: ICON_SIZE_DENSE,
  strokeWidth: ICON_STROKE,
  "aria-hidden": true,
} as const;
