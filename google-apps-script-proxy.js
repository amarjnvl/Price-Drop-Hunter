/**
 * Google Apps Script — Fetch Proxy for Price Drop Hunter
 *
 * Deploy this as a Web App:
 * 1. Go to script.google.com → New Project
 * 2. Paste this code
 * 3. Click Deploy → New Deployment → Web App
 * 4. Set "Who has access" to "Anyone"
 * 5. Click Deploy → copy the URL
 *
 * Usage: https://script.google.com/macros/s/XXXX/exec?url=https://www.flipkart.com/...
 */

function doGet(e) {
    var targetUrl = e.parameter.url;

    if (!targetUrl) {
        return ContentService.createTextOutput("Missing ?url= parameter")
            .setMimeType(ContentService.MimeType.TEXT);
    }

    try {
        var response = UrlFetchApp.fetch(targetUrl, {
            headers: {
                "User-Agent":
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept":
                    "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
            muteHttpExceptions: true,
            followRedirects: true,
        });

        return ContentService.createTextOutput(response.getContentText())
            .setMimeType(ContentService.MimeType.TEXT);
    } catch (err) {
        return ContentService.createTextOutput("ERROR: " + err.message)
            .setMimeType(ContentService.MimeType.TEXT);
    }
}
