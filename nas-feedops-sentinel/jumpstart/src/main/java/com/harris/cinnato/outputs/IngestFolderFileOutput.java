/*
 * Copyright (c) 2026 NAS FeedOps Sentinel
 *
 * Custom Jumpstart Output implementation. The built-in MessageFileOutput
 * hardcodes its write location to "./log/" (see its own "TODO change the
 * directory from config" comment) and names files with a bare millisecond
 * timestamp and no extension. Neither property is usable by this project's
 * downstream watcher: src/swim_file_ingest.py watches a configurable folder
 * (SWIM_RAW_FOLDER, normally data/raw/swim_live/) and only picks up files
 * ending in .json, .xml, or .txt.
 *
 * This class fixes both problems:
 *   - the output directory is read from config ("outputDir"), so it can be
 *     pointed at the real ingest folder instead of a fixed relative path.
 *   - each message is written with a real extension, chosen by sniffing the
 *     actual content (JSON vs. the raw STDDS/FIXM XML the SWIM broker
 *     actually sends) rather than trusting the "json" config flag blindly.
 *   - each write is atomic: content goes to a temp file in the same
 *     directory first, then an atomic rename into its final name. The
 *     downstream watcher reacts to filesystem create events the instant a
 *     file appears (watchdog.Observer, see swim_file_ingest.py), so writing
 *     the final filename directly would risk it being opened and parsed
 *     mid-write for a large message.
 *   - filenames combine the timestamp with a per-process sequence number so
 *     two messages arriving within the same millisecond never collide (the
 *     built-in MessageFileOutput has no such guard).
 */
package com.harris.cinnato.outputs;

import com.typesafe.config.Config;
import com.typesafe.config.ConfigException;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.nio.file.StandardCopyOption;
import java.util.concurrent.atomic.AtomicLong;

public class IngestFolderFileOutput extends Output {
    private static final Logger logger = LoggerFactory.getLogger(IngestFolderFileOutput.class);

    private final Path outputDirectory;
    private final AtomicLong sequence = new AtomicLong();

    /**
     * @param config the consumer config properties. Reads "outputDir"
     *               (defaults to "./log/" if absent, matching the built-in
     *               MessageFileOutput's behavior so this class is a safe
     *               drop-in even without the extra setting).
     */
    public IngestFolderFileOutput(Config config) {
        super(config);

        String dir = "./log/";
        try {
            dir = config.getString("outputDir");
        } catch (ConfigException.Missing e) {
            logger.warn("outputDir not set in config -- defaulting to {}", dir);
        }
        this.outputDirectory = Paths.get(dir);

        try {
            Files.createDirectories(this.outputDirectory);
        } catch (IOException e) {
            logger.error("Could not create output directory {}", this.outputDirectory, e);
        }
    }

    @Override
    public void output(String message, String header) {
        String body = this.convert(message);

        // convert() only produces JSON when config.json=true; otherwise the raw
        // message text (STDDS/FIXM-style XML for the live SWIM feed) passes
        // through unchanged. Sniff the actual content instead of trusting the
        // config flag, so a mismatched "json" setting can't silently produce a
        // file whose extension disagrees with what's actually inside it.
        String trimmed = body.stripLeading();
        String extension = (trimmed.startsWith("{") || trimmed.startsWith("[")) ? "json" : "xml";

        if (this.config.hasPath("headers") && this.config.getBoolean("headers")
                && header != null && !header.isEmpty()) {
            body = header + body;
        }

        String filename = String.format(
                "swim_%d_%06d.%s",
                System.currentTimeMillis(),
                sequence.incrementAndGet() % 1_000_000,
                extension);
        Path finalPath = this.outputDirectory.resolve(filename);
        Path tempPath = this.outputDirectory.resolve("." + filename + ".tmp");

        try {
            Files.write(tempPath, body.getBytes(StandardCharsets.UTF_8));
            Files.move(tempPath, finalPath, StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING);
        } catch (IOException e) {
            logger.error("Failed to write message to {}", finalPath, e);
            try {
                Files.deleteIfExists(tempPath);
            } catch (IOException cleanupError) {
                logger.warn("Failed to clean up temp file {}", tempPath, cleanupError);
            }
        }
    }
}
