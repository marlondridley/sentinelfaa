package com.harris.cinnato.solace;

import com.codahale.metrics.MetricRegistry;
import com.harris.cinnato.outputs.IngestFolderFileOutput;
import com.typesafe.config.Config;
import com.typesafe.config.ConfigFactory;

import javax.jms.Connection;
import javax.jms.Message;
import javax.jms.Queue;
import javax.jms.Session;
import javax.jms.TextMessage;
import java.lang.reflect.Proxy;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Vector;
import java.util.stream.Stream;

/**
 * Test driver for IngestFolderFileOutput (see tests/test_ingest_output_java.py).
 *
 * It drives the KIT'S OWN SolaceConsumerV1.onMessage with fake JMS messages, so what is tested is the
 * real path: the kit's catch-all around Output.output(), not a stand-in for it. It lives in the kit's
 * package because onMessage is not public. Only the two connection methods are stubbed.
 *
 *   usage: OutputHarness <mode> <outputDir> [captureDir]
 *   modes: ok | burst | badconvert | halt | softhalt | fromcapture | nogid
 */
public class OutputHarness {
    static class Consumer extends SolaceConsumerV1 {
        Consumer(Config config, com.harris.cinnato.outputs.Output output) throws Exception {
            super(config, new MetricRegistry(), output);
        }

        @Override protected Connection getConnection() { return null; }

        @Override protected Queue getQueue(Session session) { return null; }

        public JMSConsumer unused() { return null; }

        void deliver(Message message) { onMessage(message); }
    }

    static Message message(String text, Map<String, String> properties) {
        return (Message) Proxy.newProxyInstance(OutputHarness.class.getClassLoader(), new Class<?>[]{TextMessage.class},
                (proxy, method, args) -> {
                    switch (method.getName()) {
                        case "getPropertyNames": return new Vector<>(properties.keySet()).elements();
                        case "getStringProperty": return properties.get((String) args[0]);
                        case "getText": return text;
                        case "toString": return "FakeTextMessage";
                        case "hashCode": return 1;
                        case "equals": return proxy == args[0];
                        default: return null;
                    }
                });
    }

    static Map<String, String> tdesHeaders(int seq) {
        Map<String, String> p = new LinkedHashMap<>();
        p.put("mex", "TDES");
        p.put("msgType", "RD");
        p.put("globalID", "51190821");
        p.put("msgSeqID", String.valueOf(seq));
        p.put("tracon", "PCT");
        p.put("JMSXDeliveryCount", "1");
        return p;
    }

    static Config config(String dir, boolean json) {
        return ConfigFactory.parseString("outputDir=\"" + dir.replace("\\", "/") + "\"\njson=" + json
                + "\nheaders=true\nmetrics=false\noutput=x\nproviderUrl=\"tcps://harness:1\"\nqueue=q");
    }

    static void destroy(String dir) throws Exception {
        Path p = Paths.get(dir);
        try (Stream<Path> walk = Files.walk(p)) {
            walk.sorted(Comparator.reverseOrder()).forEach(x -> x.toFile().delete());
        }
        Files.writeString(p, "now a file, not a directory");
    }

    /** A capture is "Property name = k; value = v" lines, then the payload: hand it over the way the kit does. */
    static void feed(IngestFolderFileOutput output, Path capture, boolean dropIdentity) throws Exception {
        List<String> header = new ArrayList<>();
        List<String> body = new ArrayList<>();
        boolean inBody = false;
        for (String line : Files.readAllLines(capture)) {
            if (!inBody && line.startsWith("Property name = ")) {
                if (dropIdentity && (line.startsWith("Property name = globalID;") || line.startsWith("Property name = msgSeqID;"))) {
                    continue;
                }
                header.add(line);
            } else if (inBody || !line.isBlank()) {
                inBody = true;
                body.add(line);
            }
        }
        output.output(String.join("\n", body), String.join("\n", header));
    }

    public static void main(String[] a) throws Exception {
        String mode = a[0], dir = a[1];
        String xml = "<msg><airport>KBWI</airport><n>7</n></msg>";
        Config cfg = config(dir, !(mode.equals("fromcapture") || mode.equals("nogid")));
        Consumer consumer = new Consumer(cfg, new IngestFolderFileOutput(cfg));
        switch (mode) {
            case "ok":
                consumer.deliver(message(xml, tdesHeaders(1874)));
                System.out.println("DELIVERED");
                break;
            case "burst":
                for (int i = 0; i < 300; i++) consumer.deliver(message(xml, tdesHeaders(i)));
                System.out.println("DELIVERED 300");
                break;
            case "badconvert":
                consumer.deliver(message("this is not xml at all", tdesHeaders(5)));
                System.out.println("DELIVERED");
                break;
            case "halt":       // the destination breaks after startup: the real halt must fire before onMessage returns
                destroy(dir);
                consumer.deliver(message(xml, tdesHeaders(9)));
                System.out.println("RETURNED_NORMALLY");      // must never print
                break;
            case "softhalt": { // control: the same failure with the halt suppressed, i.e. what the stock kit does
                IngestFolderFileOutput soft = new IngestFolderFileOutput(cfg) {
                    @Override protected void haltProcess(int status) { System.out.println("HALT_REQUESTED"); }
                };
                Consumer softConsumer = new Consumer(cfg, soft);
                destroy(dir);
                softConsumer.deliver(message(xml, tdesHeaders(9)));
                System.out.println("RETURNED_NORMALLY");
                break;
            }
            case "fromcapture":
            case "nogid": {
                IngestFolderFileOutput output = new IngestFolderFileOutput(cfg);
                List<Path> files = new ArrayList<>();
                try (Stream<Path> s = Files.list(Paths.get(a[2]))) {
                    s.filter(Files::isRegularFile).sorted().forEach(files::add);
                }
                if (mode.equals("nogid")) files = files.subList(0, 1);
                for (Path f : files) feed(output, f, mode.equals("nogid"));
                System.out.println("FED " + files.size());
                break;
            }
            default: throw new IllegalArgumentException(mode);
        }
    }
}
