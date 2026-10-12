package com.sscam.swipetest;

import android.app.Activity;
import android.content.Context;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.Paint;
import android.os.Bundle;
import android.os.SystemClock;
import android.util.Log;
import android.view.MotionEvent;
import android.view.View;

/**
 * Minimal touch probe used by the injection benchmark.
 * Logs (tag SWIPETEST) one line per DOWN / threshold-crossing / UP, with the
 * event time converted to wall-clock microseconds so the host can compute latency.
 */
public class MainActivity extends Activity {
    static final String TAG = "SWIPETEST";

    @Override
    protected void onCreate(Bundle b) {
        super.onCreate(b);
        setContentView(new Probe(this));
    }

    static class Probe extends View {
        float x0, y0;
        boolean crossed;
        String last = "ready";
        final Paint p = new Paint();
        final float threshold;

        Probe(Context c) {
            super(c);
            p.setColor(Color.WHITE);
            p.setTextSize(90);
            threshold = 40 * c.getResources().getDisplayMetrics().density;
        }

        // Wall-clock micros at which the kernel/injector stamped this event.
        static long wallMicros(MotionEvent e) {
            long nowWallUs = System.currentTimeMillis() * 1000L;
            long ageUs = (SystemClock.uptimeNanos() - e.getEventTimeNanos()) / 1000L;
            return nowWallUs - ageUs;
        }

        static long nowMicros() {
            return System.currentTimeMillis() * 1000L;
        }

        static String dir(float dx, float dy) {
            if (Math.abs(dx) > Math.abs(dy)) return dx > 0 ? "RIGHT" : "LEFT";
            return dy > 0 ? "DOWN" : "UP";
        }

        @Override
        public boolean onTouchEvent(MotionEvent e) {
            switch (e.getActionMasked()) {
                case MotionEvent.ACTION_DOWN:
                    x0 = e.getX(); y0 = e.getY(); crossed = false;
                    Log.i(TAG, "DOWN evt_us=" + wallMicros(e) + " recv_us=" + nowMicros());
                    break;
                case MotionEvent.ACTION_MOVE:
                    float dx = e.getX() - x0, dy = e.getY() - y0;
                    if (!crossed && Math.hypot(dx, dy) >= threshold) {
                        crossed = true;
                        Log.i(TAG, "CROSS dir=" + dir(dx, dy) + " evt_us=" + wallMicros(e) + " recv_us=" + nowMicros());
                    }
                    break;
                case MotionEvent.ACTION_UP:
                    float ux = e.getX() - x0, uy = e.getY() - y0;
                    last = dir(ux, uy);
                    Log.i(TAG, "UP dir=" + last + " dx=" + (int) ux + " dy=" + (int) uy
                            + " evt_us=" + wallMicros(e) + " recv_us=" + nowMicros());
                    invalidate();
                    break;
            }
            return true;
        }

        @Override
        protected void onDraw(Canvas c) {
            c.drawColor(Color.rgb(20, 20, 60));
            c.drawText("last swipe: " + last, 60, 300, p);
        }
    }
}
