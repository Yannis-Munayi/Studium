import type { Metadata } from "next";
import { Inter, JetBrains_Mono, Source_Serif_4 } from "next/font/google";
import "@/styles/globals.css";
import { Providers } from "./providers";

/**
 * The root layout (spec §5.1, §16.1).
 *
 * Fonts are loaded through `next/font/google`, which self-hosts them at build
 * time -- no request to Google at run time, and no layout shift from a late
 * webfont, which matters more here than usual: §3 asks that streaming text not
 * reflow, and a font swapping in mid-lecture reflows every line at once.
 */

const sourceSerif = Source_Serif_4({
  subsets: ["latin"],
  display: "swap",
  variable: "--font-source-serif",
});

const inter = Inter({
  subsets: ["latin"],
  display: "swap",
  variable: "--font-inter",
});

const jetbrainsMono = JetBrains_Mono({
  subsets: ["latin"],
  display: "swap",
  variable: "--font-jetbrains-mono",
});

export const metadata: Metadata = {
  title: "Studium",
  description: "A tutor that lectures, and stops when you raise your hand.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    // §13.3 (WCAG 3.1.1): `lang` on <html>. `en-CA` per the user profile
    // default in the data layer.
    <html
      lang="en-CA"
      className={`${sourceSerif.variable} ${inter.variable} ${jetbrainsMono.variable}`}
      suppressHydrationWarning
    >
      <head>
        {/*
          The theme has to be applied before first paint or a learner who chose
          dark mode gets a white flash on every navigation. This runs
          synchronously, reads the same localStorage key `useUIStore` persists
          to, and is the one inline script in the app.
        */}
        <script
          dangerouslySetInnerHTML={{
            __html: `(function(){try{
              var s=localStorage.getItem('studium-ui');
              if(!s)return;
              var p=JSON.parse(s).state||{};
              if(p.theme==='dark'||p.theme==='light')document.documentElement.dataset.theme=p.theme;
              if(p.fontScale)document.documentElement.style.setProperty('--studium-font-scale',(p.fontScale*100)+'%');
            }catch(e){}})();`,
          }}
        />
      </head>
      <body>
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
