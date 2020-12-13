import logging
import simplejson as json
import os
from os.path import join, isfile
from queue import Queue, Empty
from threading import Thread
from typing import Optional, Tuple, List, Dict
import uuid

import astroalign as aa
from astropy.io import fits
from astropy.io.fits import ImageHDU, HDUList, Header, Card, PrimaryHDU
import numpy as np
import png
from skimage import filters, transform
from auto_stretch.stretch import Stretch

from .utils import Timer


def crop_center(img, cropx, cropy):
    y, x = img.shape
    startx = x // 2 - (cropx // 2)
    starty = y // 2 - (cropy // 2)
    return img[starty : starty + cropy, startx : startx + cropx]


class Image:
    def __init__(self, img: ImageHDU):
        self.data = img.data

        # dark file subtracted from the image; goes into the HISTORY of the fits
        self.dark: Optional[str] = None

        self.subcount = 1

        hdr = img.header

        self.camera = hdr["INSTRUME"]
        self.exp = round(float(hdr["EXPTIME"]), 2)
        self.gain = hdr["GAIN"]
        # round temp to the nearest 5 degrees
        self.temp = 5 * round(float(hdr["CCD-TEMP"]) / 5)

        image_type = hdr["IMAGETYP"]

        self.subcount = hdr.get("SUBCOUNT") or 1

        if str(image_type).lower().find("light") >= 0:
            self.image_type = "LIGHT"
            self.target = hdr["OBJECT"]
            self.filter = hdr["FILTER"]

        elif str(image_type).lower().find("dark") >= 0:
            self.image_type = "DARK"
            self.target = None
            self.filter = None

        elif str(image_type).lower().find("flat") >= 0:
            self.image_type = "FLAT"
            self.filter = hdr["FILTER"]
            self.target = None

    def __iter__(self):
        yield "camera", self.camera
        yield "exp", self.exp
        yield "gain", self.gain
        yield "temp", self.temp
        yield "image_type", self.image_type
        yield "target", self.target
        yield "filter", self.filter
        yield "key", self.key
        yield "dark_key", self.dark_key
        yield "flat_key", self.flat_key

    @property
    def key(self) -> Optional[str]:
        if self.image_type == "LIGHT":
            return f"{self.camera}_{self.image_type}_{self.target}_{self.filter}_{self.exp}_{self.gain}_{self.temp}"
        elif self.image_type == "DARK":
            return f"{self.camera}_{self.image_type}_{self.exp}_{self.gain}_{self.temp}"
        elif self.image_type == "FLAT":
            return (
                f"{self.camera}_{self.image_type}_{self.filter}_{self.gain}_{self.temp}"
            )
        return None

    @property
    def dark_key(self) -> Optional[str]:
        if self.image_type == "LIGHT" or self.image_type == "FLAT":
            return f"{self.camera}_DARK_{self.exp}_{self.gain}_{self.temp}"
        return None

    @property
    def flat_key(self) -> Optional[str]:
        if self.image_type == "LIGHT":
            return f"{self.camera}_FLAT_{self.filter}_{self.gain}_{self.temp}"
        return None

    @property
    def fits_header(self) -> Header:
        hdr = Header()

        hdr.set("INSTRUME", self.camera)
        hdr.set("EXPTIME", self.exp)
        hdr.set("GAIN", self.gain)
        hdr.set("CCD-TEMP", self.temp)

        if self.image_type == "LIGHT":
            hdr.set("IMAGETYP", "Light Frame")
            hdr.set("OBJECT", self.target)
            hdr.set("FILTER", self.filter)
        elif self.image_type == "FLAT":
            hdr.set("IMAGETYP", "Flat Frame")
            hdr.set("FILTER", self.filter)
        elif self.image_type == "DARK":
            hdr.set("IMAGETYP", "Dark Frame")

        hdr.set("SUBCOUNT", self.subcount)

        if self.dark is not None:
            hdr.add_history(f"dark {self.dark}")

        return hdr

    def set_dark(self, path: str):
        self.dark = path

    def save_fits(self, folder: str) -> str:
        # ensure we always write 16bit fits files
        data = self.data.copy()
        data = np.uint16(np.interp(data, (data.min(), data.max()), (0, 65535)))

        hdu = PrimaryHDU(
            data=self.data,
            header=self.fits_header,
        )
        l = HDUList([hdu])
        path = join(folder, f"{self.key}.fits")
        l.writeto(path, overwrite=True)
        return path

    def save_stretched_png(self, folder: str) -> str:
        data = self.data.copy()
        data = np.uint16(np.interp(data, (data.min(), data.max()), (0, 65535)))

        data = crop_center(data, data.shape[1] - 128, data.shape[0] - 128)

        with Timer("stretch"):
            data = Stretch().stretch(data)

        smaller = transform.downscale_local_mean(data, (4, 4))

        smaller = np.uint16(
            np.interp(smaller, (smaller.min(), smaller.max()), (0, 65535))
        )

        with Timer(f"saving {self.key}.png"):
            path = join(folder, f"{self.key}.png")
            png_image = png.from_array(smaller.copy(), "L")
            png_image.save(path)

        return path


class DB:
    def __init__(self, folder: str):
        self.folder = folder
        self.processed: List[str] = []

        if isfile(join(folder, "processed.txt")):
            with open(join(folder, "processed.txt")) as f:
                self.processed = [line.rstrip() for line in f]

    def is_already_processed(self, path: str) -> bool:
        return path in self.processed

    def stack_exists(self, img: Image) -> bool:
        return os.path.isfile(join(self.folder, f"{img.key}.fits"))

    def mark_processed(self, path: str):
        self.processed.append(path)
        with open(join(self.folder, "processed.txt"), "a+") as f:
            f.write(f"{path}\n")
            f.flush()

    def get_stacked_image(self, key: str) -> Optional[Image]:
        try:
            with fits.open(join(self.folder, f"{key}.fits")) as f:
                return Image(f[0])
        except:
            return None


class Stacker:
    def __init__(self, storage_folder: str, output_folder: str):
        self.storage_folder = storage_folder
        self.output_folder = output_folder
        self.queue: Queue = Queue()
        self.thread = None
        self.db = DB(self.storage_folder)
        self._stop = False
        self.output_queues: Dict[str, Queue] = {}

        os.makedirs(self.storage_folder, exist_ok=True)
        os.makedirs(self.output_folder, exist_ok=True)

    def add_output_queue(self, q: Queue) -> str:
        id = str(uuid.uuid4())
        self.output_queues[id] = q
        return id

    def remove_output_queue(self, id: str):
        del self.output_queues[id]

    def start(self):
        if self.thread:
            return

        self.thread = Thread(target=self._worker)
        self.thread.start()

    def stop(self):
        self._stop = True
        self.thread.join()

    def stack_image(self, path: str):
        self.queue.put(path)

    def _process_item(self, path: str):
        if self.db.is_already_processed(path):
            logging.info(f"skipping already processed file {path}")
            return

        with Timer(f"processing file {path}"):
            with fits.open(path) as fit:
                img = Image(fit[0])

            # always mark it as processed. if we error out, we don't want to keep
            # erroring on the same file
            self.db.mark_processed(path)

            if not self.db.stack_exists(img):
                stacked_path = img.save_fits(self.storage_folder)
                img.save_stretched_png(self.output_folder)
            else:
                if img.image_type == "LIGHT":
                    img = self._subtract_dark(img)
                    img.data = self._divide_flat(img)
                    img.data = self._align(img)
                    stacked = self._stack(img)

                    png_path = stacked.save_stretched_png(self.output_folder)
                    for q in self.output_queues.values():
                        q.put(png_path)

                elif img.image_type == "DARK":
                    self._stack(img)
                elif img.image_type == "FLAT":
                    img = self._subtract_dark(img)
                    self._stack(img)

    def _subtract_dark(self, img: Image) -> Image:
        dark = self.db.get_stacked_image(str(img.dark_key))
        if dark is None:
            logging.info(f"no dark found for {img.dark_key}")
            return img

        with Timer(f"subtracting dark for {img.dark_key}"):
            img.data = img.data - dark.data
            img.set_dark(str(img.dark_key))
        return img

    def _divide_flat(self, img: Image) -> np.array:
        flat = self.db.get_stacked_image(str(img.flat_key))
        if flat is None:
            logging.info(f"no flat found for {img.flat_key}")
            return img.data

        with Timer(f"dividing flat for {img.flat_key}"):
            return img.data / (flat.data / flat.data.mean())

    def _align(self, img: Image) -> np.array:
        with Timer(f"aligning image for {img.key}"):
            reference = self.db.get_stacked_image(str(img.key))
            registered, footprint = aa.register(img.data, reference, fill_value=0)
            return registered

    def _stack(self, img: Image):
        with Timer(f"stacking image for {img.key}"):
            stacked = self.db.get_stacked_image(str(img.key))

            if stacked is None:
                raise Exception("expected a stack")

            if img.image_type == "LIGHT":
                data = img.data
            else:
                data = filters.gaussian(img.data)

            count = stacked.subcount

            stacked.data = (count * stacked.data + data) / (count + 1)

        stacked.subcount += 1

        with Timer(f"saving stacked fits for {img.key}"):
            stacked.save_fits(self.storage_folder)

        return stacked

    def _worker(self):
        while not self._stop:
            try:
                item = self.queue.get(timeout=1)
            except Empty:
                continue

            try:
                self._process_item(item)
            except Exception as e:
                logging.error(e)
            finally:
                self.queue.task_done()

            logging.info(f"{self.queue.qsize()} items remaining")
