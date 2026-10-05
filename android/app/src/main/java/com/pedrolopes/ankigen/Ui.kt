package com.pedrolopes.ankigen

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.IntrinsicSize
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.SpanStyle
import androidx.compose.ui.text.buildAnnotatedString
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.text.withStyle
import androidx.compose.ui.unit.dp
import java.time.Instant
import java.time.ZoneId
import java.time.ZonedDateTime
import java.time.format.DateTimeFormatter

private val Corner = RoundedCornerShape(2.dp)

@Composable
fun Kicker(text: String, modifier: Modifier = Modifier, color: Color = ink(0.5f)) {
    Text(text.uppercase(), modifier, color, style = KickerStyle, maxLines = 1, overflow = TextOverflow.Ellipsis)
}

@Composable
fun Rule(modifier: Modifier = Modifier, color: Color = Divider) {
    Box(modifier.fillMaxWidth().height(1.dp).background(color))
}

@Composable
fun PrimaryButton(label: String, onClick: () -> Unit, modifier: Modifier = Modifier, enabled: Boolean = true) {
    Box(
        modifier
            .height(44.dp)
            .background(if (enabled) Cyan else Cyan.copy(alpha = 0.35f), Corner)
            .clickable(enabled = enabled, onClick = onClick)
            .padding(horizontal = 18.dp),
        contentAlignment = Alignment.Center,
    ) { Text(label, color = Paper, style = MaterialTheme.typography.labelLarge) }
}

@Composable
fun SecondaryButton(label: String, onClick: () -> Unit, modifier: Modifier = Modifier, color: Color = Ink) {
    Box(
        modifier
            .height(44.dp)
            .border(1.dp, Divider, Corner)
            .clickable(onClick = onClick)
            .padding(horizontal = 18.dp),
        contentAlignment = Alignment.Center,
    ) { Text(label, color = color, style = MaterialTheme.typography.labelLarge) }
}

/** Words in a row, the chosen one cyan and underlined: Broadsheet has no chips. */
@Composable
fun Tabs(tabs: List<String>, selected: Int, onSelect: (Int) -> Unit, modifier: Modifier = Modifier) {
    Row(modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(18.dp)) {
        tabs.forEachIndexed { i, label ->
            Column(
                Modifier.width(IntrinsicSize.Max).clickable { onSelect(i) }.padding(vertical = 6.dp),
                horizontalAlignment = Alignment.CenterHorizontally,
            ) {
                Text(
                    label,
                    style = MaterialTheme.typography.labelLarge,
                    color = if (i == selected) Cyan else ink(0.45f),
                )
                Spacer(Modifier.height(3.dp))
                Box(
                    Modifier.fillMaxWidth().height(1.5.dp)
                        .background(if (i == selected) Cyan else Color.Transparent),
                )
            }
        }
    }
}

/** A surface with a coloured top rule: cyan went well, magenta did not. */
@Composable
fun Slip(rule: Color, modifier: Modifier = Modifier, content: @Composable () -> Unit) {
    Column(modifier.fillMaxWidth().background(PaperSurface, Corner)) {
        Box(Modifier.fillMaxWidth().height(2.dp).background(rule))
        Column(Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) { content() }
    }
}

@Composable
fun Spinner(modifier: Modifier = Modifier) {
    CircularProgressIndicator(modifier.size(18.dp), strokeWidth = 2.dp, color = Cyan)
}

/** A label and its value on one line. */
@Composable
fun Fact(label: String, value: String, valueColor: Color = Ink) {
    Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.Bottom) {
        Kicker(label, Modifier.width(110.dp))
        Text(value, style = MaterialTheme.typography.bodyMedium, color = valueColor)
    }
}

/** Card text marks code with backticks; set it in monospace. */
fun codeSpans(text: String): AnnotatedString = buildAnnotatedString {
    text.split('`').forEachIndexed { i, part ->
        if (i % 2 == 1) {
            withStyle(SpanStyle(fontFamily = Mono, background = ink(0.07f))) { append(part) }
        } else {
            append(part)
        }
    }
}

val Bold = SpanStyle(fontWeight = FontWeight.SemiBold)

private val When = DateTimeFormatter.ofPattern("EEE d MMM, HH:mm")

/** An ISO time in the phone's zone, e.g. "Mon 5 Oct, 05:23". */
fun local(iso: String?): String = iso?.let {
    runCatching { When.format(Instant.parse(it).atZone(ZoneId.systemDefault())) }.getOrDefault(it)
} ?: "—"

fun local(time: ZonedDateTime): String = When.format(time.withZoneSameInstant(ZoneId.systemDefault()))
