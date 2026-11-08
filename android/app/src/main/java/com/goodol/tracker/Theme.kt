package com.goodol.tracker

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Typography
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.Immutable
import androidx.compose.runtime.staticCompositionLocalOf
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.sp

/*
 * "Calm focus" palette, the same family as the PC dashboard:
 *   midnight ink surfaces · blue = focus / action · mint = done, goals met
 *   amber = heads-up (limit close, setup needed) · coral = over a limit, blocked
 * Fixed colours (not the wallpaper's), so meaning stays the same on every phone.
 */

@Immutable
data class Signal(
    val success: Color, val successContainer: Color, val onSuccessContainer: Color,
    val warning: Color, val warningContainer: Color, val onWarningContainer: Color,
    val track: Color,
)

private val DarkSignal = Signal(
    success = Color(0xFF4FD1A5), successContainer = Color(0xFF123A30), onSuccessContainer = Color(0xFFBFF3DF),
    warning = Color(0xFFF5C451), warningContainer = Color(0xFF3A2E12), onWarningContainer = Color(0xFFFCE7B0),
    track = Color(0xFF2A303B),
)
private val LightSignal = Signal(
    success = Color(0xFF0F9D74), successContainer = Color(0xFFD5F5E9), onSuccessContainer = Color(0xFF053D2D),
    warning = Color(0xFFB7791F), warningContainer = Color(0xFFFDF0D2), onWarningContainer = Color(0xFF4A3005),
    track = Color(0xFFE3E7EE),
)

private val Dark = darkColorScheme(
    primary = Color(0xFF6EA8FE), onPrimary = Color(0xFF07162E),
    primaryContainer = Color(0xFF1A3358), onPrimaryContainer = Color(0xFFD6E6FF),
    secondary = Color(0xFF4FD1A5), onSecondary = Color(0xFF05271D),
    secondaryContainer = Color(0xFF123A30), onSecondaryContainer = Color(0xFFBFF3DF),
    tertiary = Color(0xFFF5C451), onTertiary = Color(0xFF2B2006),
    tertiaryContainer = Color(0xFF3A2E12), onTertiaryContainer = Color(0xFFFCE7B0),
    error = Color(0xFFFF7A7A), onError = Color(0xFF3B0A0C),
    errorContainer = Color(0xFF4A1D20), onErrorContainer = Color(0xFFFFDADA),
    background = Color(0xFF0E1116), onBackground = Color(0xFFE8EAF0),
    surface = Color(0xFF0E1116), onSurface = Color(0xFFE8EAF0),
    surfaceVariant = Color(0xFF1D222B), onSurfaceVariant = Color(0xFF9AA3B2),
    surfaceContainerLowest = Color(0xFF0B0D11), surfaceContainerLow = Color(0xFF13171D),
    surfaceContainer = Color(0xFF161A21), surfaceContainerHigh = Color(0xFF1D222B),
    surfaceContainerHighest = Color(0xFF242A35),
    outline = Color(0xFF3A4250), outlineVariant = Color(0xFF262C37),
)

private val Light = lightColorScheme(
    primary = Color(0xFF2563EB), onPrimary = Color.White,
    primaryContainer = Color(0xFFDCE7FF), onPrimaryContainer = Color(0xFF0B2A66),
    secondary = Color(0xFF0F9D74), onSecondary = Color.White,
    secondaryContainer = Color(0xFFD5F5E9), onSecondaryContainer = Color(0xFF053D2D),
    tertiary = Color(0xFFB7791F), onTertiary = Color.White,
    tertiaryContainer = Color(0xFFFDF0D2), onTertiaryContainer = Color(0xFF4A3005),
    error = Color(0xFFD64545), onError = Color.White,
    errorContainer = Color(0xFFFCE1E1), onErrorContainer = Color(0xFF5C1010),
    background = Color(0xFFF5F7FB), onBackground = Color(0xFF121620),
    surface = Color(0xFFF5F7FB), onSurface = Color(0xFF121620),
    surfaceVariant = Color(0xFFEEF1F6), onSurfaceVariant = Color(0xFF5B6475),
    surfaceContainerLowest = Color.White, surfaceContainerLow = Color(0xFFFBFCFE),
    surfaceContainer = Color.White, surfaceContainerHigh = Color(0xFFF1F4F9),
    surfaceContainerHighest = Color(0xFFE9EDF4),
    outline = Color(0xFFC9D0DB), outlineVariant = Color(0xFFE1E6EE),
)

val LocalSignal = staticCompositionLocalOf { DarkSignal }

private val AppType = Typography().let { t ->
    t.copy(
        headlineSmall = t.headlineSmall.copy(fontWeight = FontWeight.SemiBold, letterSpacing = (-0.2).sp),
        titleMedium = t.titleMedium.copy(fontWeight = FontWeight.SemiBold),
        labelSmall = TextStyle(fontSize = 11.sp, fontWeight = FontWeight.SemiBold, letterSpacing = 1.2.sp),
    )
}

@Composable
fun AppTheme(content: @Composable () -> Unit) {
    val dark = isSystemInDarkTheme()
    CompositionLocalProvider(LocalSignal provides if (dark) DarkSignal else LightSignal) {
        MaterialTheme(colorScheme = if (dark) Dark else Light, typography = AppType, content = content)
    }
}
