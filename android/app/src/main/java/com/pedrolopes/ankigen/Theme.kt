package com.pedrolopes.ankigen

import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Typography
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.sp

/**
 * Broadsheet, the design system the first app used: a paper ground, one ink,
 * cyan as the only interactive colour and magenta only for failures.
 */
val Paper = Color(0xFFF3F2F2)
val PaperSurface = Color(0xFFEAE9E9)
val Ink = Color(0xFF201E1D)
val Cyan = Color(0xFF0088B0)
val Cyan700 = Color(0xFF006786)
val Magenta = Color(0xFFD6006C)

fun ink(fraction: Float): Color = Ink.copy(alpha = fraction)

val Divider = ink(0.16f)

// The platform serif stands in for Source Serif 4: same genre, no font binary.
val Serif: FontFamily = FontFamily.Serif
val Mono: FontFamily = FontFamily.Monospace

private fun heading(size: Int, tracking: Float = -0.015f) = TextStyle(
    fontFamily = Serif, fontWeight = FontWeight.SemiBold,
    fontSize = size.sp, lineHeight = (size * 1.15f).sp, letterSpacing = (size * tracking).sp,
)

private fun body(size: Int, lineHeight: Float = 1.5f) = TextStyle(
    fontFamily = Serif, fontSize = size.sp, lineHeight = (size * lineHeight).sp,
)

/** The system's small-caps label, uppercased where it is used. */
val KickerStyle = TextStyle(fontFamily = Serif, fontSize = 10.sp, lineHeight = 13.sp, letterSpacing = 1.1.sp)

private val Type = Typography(
    headlineLarge = heading(32, tracking = -0.02f),
    headlineSmall = heading(25),
    titleLarge = heading(23, tracking = -0.02f),
    titleMedium = heading(18),
    titleSmall = heading(15),
    bodyLarge = body(15, lineHeight = 1.55f),
    bodyMedium = body(13),
    bodySmall = body(12),
    labelLarge = TextStyle(fontFamily = Serif, fontWeight = FontWeight.SemiBold, fontSize = 14.sp),
    labelSmall = KickerStyle,
)

@Composable
fun AnkiGenTheme(content: @Composable () -> Unit) {
    // A paper system with no dark counterpart, so it ignores the system setting.
    MaterialTheme(
        colorScheme = lightColorScheme(
            primary = Cyan, onPrimary = Paper,
            secondary = Magenta, onSecondary = Paper,
            background = Paper, onBackground = Ink,
            surface = Paper, onSurface = Ink,
            surfaceVariant = PaperSurface, onSurfaceVariant = ink(0.62f),
            outline = Divider, outlineVariant = Divider,
            error = Magenta, onError = Paper,
        ),
        typography = Type,
        content = content,
    )
}
