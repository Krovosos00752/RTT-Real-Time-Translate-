import threading
import time
import tkinter as tk
from dataclasses import dataclass
from tkinter import ttk

import mss
import pytesseract
from deep_translator import GoogleTranslator
from PIL import Image


@dataclass
class OCRLine:
    text: str
    x: int
    y: int
    w: int
    h: int


class ScreenCaptureService:
    def grab(self, region: dict) -> Image.Image:
        with mss.mss() as sct:
            raw = sct.grab(region)
            return Image.frombytes("RGB", raw.size, raw.rgb)


class OCRService:
    def extract_lines(self, image: Image.Image, lang: str = "eng") -> list[OCRLine]:
        data = pytesseract.image_to_data(image, lang=lang, output_type=pytesseract.Output.DICT)

        lines: list[OCRLine] = []
        n = len(data["text"])
        for i in range(n):
            text = (data["text"][i] or "").strip()
            conf = data["conf"][i]
            if not text or conf == "-1":
                continue
            try:
                if float(conf) < 45:
                    continue
            except ValueError:
                continue

            lines.append(
                OCRLine(
                    text=text,
                    x=int(data["left"][i]),
                    y=int(data["top"][i]),
                    w=int(data["width"][i]),
                    h=int(data["height"][i]),
                )
            )

        lines.sort(key=lambda l: (l.y, l.x))
        return lines


class TextSegmenter:
    def group_into_blocks(self, lines: list[OCRLine], y_gap: int = 18) -> list[str]:
        if not lines:
            return []

        blocks: list[list[OCRLine]] = [[lines[0]]]
        for curr in lines[1:]:
            prev = blocks[-1][-1]
            if abs(curr.y - prev.y) <= y_gap:
                blocks[-1].append(curr)
            else:
                blocks.append([curr])

        result: list[str] = []
        for block in blocks:
            txt = " ".join(item.text for item in block).strip()
            if txt:
                result.append(txt)
        return result


class TranslationService:
    def __init__(self) -> None:
        self._translator_cache: dict[tuple[str, str], GoogleTranslator] = {}

    def translate(self, text: str, src: str, target: str) -> str:
        key = (src, target)
        translator = self._translator_cache.get(key)
        if translator is None:
            translator = GoogleTranslator(source=src, target=target)
            self._translator_cache[key] = translator
        return translator.translate(text)


class TranslatorApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("RTT Real-Time Translate (MVP)")
        self.root.geometry("900x650")

        self.capture_service = ScreenCaptureService()
        self.ocr_service = OCRService()
        self.segmenter = TextSegmenter()
        self.translator = TranslationService()

        self.running = False
        self.worker_thread: threading.Thread | None = None
        self.last_rendered_signature = ""
        self.monitor_choice = tk.StringVar(value="1")

        self._build_ui()

    def _build_ui(self) -> None:
        controls = ttk.Frame(self.root, padding=10)
        controls.pack(fill=tk.X)

        ttk.Label(controls, text="OCR язык (tesseract):").grid(row=0, column=0, sticky=tk.W, padx=(0, 6))
        self.ocr_lang = tk.StringVar(value="eng")
        ttk.Entry(controls, textvariable=self.ocr_lang, width=10).grid(row=0, column=1, sticky=tk.W)

        ttk.Label(controls, text="Из языка:").grid(row=0, column=2, sticky=tk.W, padx=(12, 6))
        self.src_lang = tk.StringVar(value="en")
        ttk.Entry(controls, textvariable=self.src_lang, width=8).grid(row=0, column=3, sticky=tk.W)

        ttk.Label(controls, text="В язык:").grid(row=0, column=4, sticky=tk.W, padx=(12, 6))
        self.dst_lang = tk.StringVar(value="ru")
        ttk.Entry(controls, textvariable=self.dst_lang, width=8).grid(row=0, column=5, sticky=tk.W)

        ttk.Label(controls, text="Интервал (сек):").grid(row=0, column=6, sticky=tk.W, padx=(12, 6))
        self.interval = tk.StringVar(value="1.0")
        ttk.Entry(controls, textvariable=self.interval, width=6).grid(row=0, column=7, sticky=tk.W)

        ttk.Label(controls, text="Монитор:").grid(row=0, column=8, sticky=tk.W, padx=(12, 6))
        self.monitor_box = ttk.Combobox(
            controls,
            textvariable=self.monitor_choice,
            state="readonly",
            width=5,
            values=self._available_monitors(),
        )
        self.monitor_box.grid(row=0, column=9, sticky=tk.W)
        if self.monitor_box["values"]:
            self.monitor_box.current(0)

        self.start_btn = ttk.Button(controls, text="Запустить", command=self.start)
        self.start_btn.grid(row=0, column=10, padx=(16, 6))

        self.stop_btn = ttk.Button(controls, text="Остановить", command=self.stop, state=tk.DISABLED)
        self.stop_btn.grid(row=0, column=11)

        self.status = tk.StringVar(value="Готово")
        ttk.Label(self.root, textvariable=self.status, padding=(10, 0)).pack(anchor=tk.W)

        self.output = tk.Text(self.root, wrap=tk.WORD, font=("Arial", 13))
        self.output.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        self.output.configure(state=tk.DISABLED)

    def _available_monitors(self) -> list[str]:
        with mss.mss() as sct:
            return [str(i) for i in range(1, len(sct.monitors))]

    def start(self) -> None:
        if self.running:
            return

        self.running = True
        self.start_btn.configure(state=tk.DISABLED)
        self.stop_btn.configure(state=tk.NORMAL)
        self.status.set("Запущено")

        self.worker_thread = threading.Thread(target=self._run_loop, daemon=True)
        self.worker_thread.start()

    def stop(self) -> None:
        self.running = False
        self.start_btn.configure(state=tk.NORMAL)
        self.stop_btn.configure(state=tk.DISABLED)
        self.status.set("Остановлено")

    def _run_loop(self) -> None:
        while self.running:
            try:
                with mss.mss() as sct:
                    monitors = sct.monitors
                    selected_monitor = self._selected_monitor_index(len(monitors))
                    monitor = monitors[selected_monitor]
                region = {
                    "left": monitor["left"],
                    "top": monitor["top"],
                    "width": monitor["width"],
                    "height": monitor["height"],
                }

                image = self.capture_service.grab(region)
                lines = self.ocr_service.extract_lines(image, lang=self.ocr_lang.get().strip() or "eng")
                blocks = self.segmenter.group_into_blocks(lines)

                if not blocks:
                    self._set_status("Текст не найден")
                    time.sleep(self._interval_seconds())
                    continue

                signature = "\n".join(blocks)
                if signature == self.last_rendered_signature:
                    self._set_status("Без изменений")
                    time.sleep(self._interval_seconds())
                    continue

                translated_blocks = []
                for block in blocks:
                    translated = self.translator.translate(
                        block,
                        src=self.src_lang.get().strip() or "auto",
                        target=self.dst_lang.get().strip() or "ru",
                    )
                    translated_blocks.append(translated)

                self.last_rendered_signature = signature
                self._render_blocks(translated_blocks)
                self._set_status(f"Обновлено: {time.strftime('%H:%M:%S')}")
            except Exception as exc:
                self._set_status(f"Ошибка: {exc}")

            time.sleep(self._interval_seconds())

    def _interval_seconds(self) -> float:
        try:
            value = float(self.interval.get())
            return max(0.2, value)
        except ValueError:
            return 1.0

    def _render_blocks(self, blocks: list[str]) -> None:
        def update() -> None:
            self.output.configure(state=tk.NORMAL)
            self.output.delete("1.0", tk.END)
            for i, block in enumerate(blocks, start=1):
                self.output.insert(tk.END, f"[{i}] {block}\n\n")
            self.output.configure(state=tk.DISABLED)

        self.root.after(0, update)

    def _set_status(self, text: str) -> None:
        self.root.after(0, lambda: self.status.set(text))

    def _selected_monitor_index(self, monitor_count: int) -> int:
        if monitor_count <= 1:
            return 0

        try:
            selected = int(self.monitor_choice.get())
        except ValueError:
            return 1

        if selected < 1 or selected >= monitor_count:
            return 1
        return selected


def main() -> None:
    root = tk.Tk()
    app = TranslatorApp(root)
    root.protocol("WM_DELETE_WINDOW", lambda: (app.stop(), root.destroy()))
    root.mainloop()


if __name__ == "__main__":
    main()
