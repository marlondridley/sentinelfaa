/*
 * Copyright (c) 2026 NAS FeedOps Sentinel
 *
 * Custom Jumpstart Output: one durable, structured record per SWIM message.
 *
 * Why this exists. The kit's consumer (SolaceConsumerV1/V2) uses
 * AUTO_ACKNOWLEDGE and its onMessage() catches every exception, so the broker
 * is told a message was handled whether or not the Output managed to write it,
 * and the built-in MessageFileOutput logs a failed write and returns. It also
 * names files by System.currentTimeMillis() and truncates on open, so two
 * messages in one millisecond overwrite each other. The result is a broker ack
 * with nothing on disk.
 *
 * What this class guarantees. A message is acknowledged only after its record
 * is on disk:
 *   - the record goes to a hidden temp file, is fsync'ed, and is renamed into
 *     place atomically, so the Python side never sees a partial file;
 *   - names are unique (run id + sequence + arrival millis);
 *   - if the record cannot be made durable, the JVM is halted BEFORE this
 *     method returns. onMessage() never returns, AUTO_ACKNOWLEDGE never
 *     acknowledges, and the broker redelivers the message to the next
 *     connection. The launcher (jumpstart/run-live.ps1) restarts the kit.
 *   The only thing that is deliberately NOT fatal is a payload that cannot be
 *   converted to JSON: that is a data problem, not a delivery problem, so the
 *   original text is preserved with a convert_error and the Python side
 *   quarantines it with a reason. Halting on it would redeliver it forever.
 *
 * The record (the seam between the JVM edge and the Python side) is a single
 * JSON object in a file named swim_<run>_<seq>_<arrival millis>.swimmsg:
 *   {"format": "swim-raw/1", "message_id": "...", "received_at": "<ISO-8601 UTC>",
 *    "headers": {"<JMS property>": "<value>", ...}, "body": "<payload text>",
 *    "convert_error": "<only when JSON conversion failed>"}
 * message_id is the SWIM identity mex:globalID:msgSeqID when the message
 * carries one (the same rule as src/raw_message.py), else sha256:<hex> of the
 * raw payload, so a redelivery has the same id.
 *
 * Build and run: see jumpstart/run-live.ps1 (compiles this class against the
 * kit's jar and starts com.harris.cinnato.Consumer with both on the classpath).
 */
package com.harris.cinnato.outputs;

import com.typesafe.config.Config;
import com.typesafe.config.ConfigException;
import org.json.JSONObject;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.IOException;
import java.nio.ByteBuffer;
import java.nio.channels.FileChannel;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.nio.file.StandardCopyOption;
import java.nio.file.StandardOpenOption;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.time.Instant;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.ThreadLocalRandom;
import java.util.concurrent.atomic.AtomicLong;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

public class IngestFolderFileOutput extends Output {
    private static final Logger logger = LoggerFactory.getLogger(IngestFolderFileOutput.class);

    /** Version of the record layout; the Python reader refuses formats it does not know. */
    public static final String FORMAT = "swim-raw/1";
    public static final String EXTENSION = ".swimmsg";

    private static final Pattern PROPERTY = Pattern.compile("^Property name = ([^;]+);\\s*value = (.*)$");

    private final Path outputDirectory;
    private final AtomicLong sequence = new AtomicLong();
    private final String runId = String.format("%04x", ThreadLocalRandom.current().nextInt(0x10000));

    /**
     * @param config the consumer config. "outputDir" is required: a kit that cannot say where it
     *               writes must not start, because it would acknowledge messages it cannot keep.
     */
    public IngestFolderFileOutput(Config config) {
        super(config);
        String dir;
        try {
            dir = config.getString("outputDir");
        } catch (ConfigException.Missing e) {
            logger.error("outputDir is not set: refusing to start, since nothing could be written");
            throw new IllegalStateException("outputDir is required", e);
        }
        this.outputDirectory = Paths.get(dir);
        try {
            Files.createDirectories(this.outputDirectory);
            // Prove the directory is writable now, not at the first message.
            Path probe = Files.createTempFile(this.outputDirectory, ".probe-", ".tmp");
            Files.deleteIfExists(probe);
        } catch (IOException e) {
            logger.error("Cannot write to {}: refusing to start", this.outputDirectory, e);
            throw new IllegalStateException("outputDir is not writable: " + this.outputDirectory, e);
        }
    }

    @Override
    public void output(String message, String header) {
        Instant receivedAt = Instant.now();
        Map<String, String> headers = parseHeaders(header);

        String body;
        String convertError = null;
        try {
            body = this.convert(message);
            // org.json's XML converter does not throw on text that is not XML: it returns an empty
            // object, which would silently replace the payload. Treat that as a failed conversion.
            if ("{}".equals(body) && !message.isBlank()) {
                body = message;
                convertError = "payload converted to an empty JSON object (is it XML?)";
                logger.warn("{}; keeping the original text", convertError);
            }
        } catch (RuntimeException e) {
            // A payload we cannot convert is preserved as received, and flagged.
            body = message;
            convertError = e.getClass().getSimpleName() + ": " + e.getMessage();
            logger.warn("Payload could not be converted to JSON; keeping the original text", e);
        }

        JSONObject record = new JSONObject();
        record.put("format", FORMAT);
        record.put("message_id", messageId(headers, message));
        record.put("received_at", receivedAt.toString());
        record.put("headers", new JSONObject(headers));
        record.put("body", body);
        if (convertError != null) {
            record.put("convert_error", convertError);
        }
        persist(record.toString().getBytes(StandardCharsets.UTF_8), receivedAt);
    }

    /** Durably write one record, or stop the process. Never returns on failure. */
    private void persist(byte[] bytes, Instant receivedAt) {
        String name = String.format("swim_%s_%06d_%d%s",
                runId, sequence.incrementAndGet() % 1_000_000, receivedAt.toEpochMilli(), EXTENSION);
        Path finalPath = this.outputDirectory.resolve(name);
        Path tempPath = this.outputDirectory.resolve("." + name + ".tmp");
        try {
            try (FileChannel channel = FileChannel.open(tempPath,
                    StandardOpenOption.CREATE_NEW, StandardOpenOption.WRITE)) {
                ByteBuffer buffer = ByteBuffer.wrap(bytes);
                while (buffer.hasRemaining()) {
                    channel.write(buffer);
                }
                channel.force(true);            // on disk before it can be renamed into view
            }
            Files.move(tempPath, finalPath, StandardCopyOption.ATOMIC_MOVE);
        } catch (IOException | RuntimeException e) {
            logger.error("Could not make {} durable: stopping so the broker redelivers this message", finalPath, e);
            try {
                Files.deleteIfExists(tempPath);
            } catch (IOException ignored) {
                // best effort: the process is about to stop
            }
            haltProcess(1);
            // haltProcess does not return; if a subclass's override does, do not fall through as success.
            throw new IllegalStateException("record was not persisted", e);
        }
    }

    /**
     * Stop now, without running shutdown hooks or acknowledging anything. Overridable so a test can
     * observe the decision; in production it ends the JVM.
     */
    protected void haltProcess(int status) {
        System.err.flush();
        Runtime.getRuntime().halt(status);
    }

    /** The kit hands headers over as "Property name = k; value = v" lines; turn them back into a map. */
    static Map<String, String> parseHeaders(String header) {
        Map<String, String> headers = new LinkedHashMap<>();
        if (header == null || header.isEmpty()) {
            return headers;
        }
        String key = null;
        StringBuilder value = null;
        for (String line : header.split("\n", -1)) {
            Matcher m = PROPERTY.matcher(line);
            if (m.matches()) {
                if (key != null) {
                    headers.put(key, value.toString().strip());
                }
                key = m.group(1).strip();
                value = new StringBuilder(m.group(2));
            } else if (key != null) {
                value.append('\n').append(line);        // a value that itself spans lines
            }
        }
        if (key != null) {
            headers.put(key, value.toString().strip());
        }
        return headers;
    }

    /** Same rule as src/raw_message.py: the SWIM identity if there is one, else a hash of the payload. */
    static String messageId(Map<String, String> headers, String rawBody) {
        String globalId = headers.getOrDefault("globalID", "");
        String seqId = headers.getOrDefault("msgSeqID", "");
        if (!globalId.isEmpty() || !seqId.isEmpty()) {
            return headers.getOrDefault("mex", "") + ":" + globalId + ":" + seqId;
        }
        try {
            byte[] digest = MessageDigest.getInstance("SHA-256").digest(rawBody.getBytes(StandardCharsets.UTF_8));
            StringBuilder hex = new StringBuilder("sha256:");
            for (byte b : digest) {
                hex.append(String.format("%02x", b));
            }
            return hex.toString();
        } catch (NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }
}
