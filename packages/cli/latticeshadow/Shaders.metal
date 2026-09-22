#include <metal_stdlib>
using namespace metal;

struct VertexOut {
    float4 position [[position]];
    float2 uv;
};

vertex VertexOut vertex_main(uint vertex_id [[vertex_id]]) {
    VertexOut out;
    float2 grid[4] = {
        float2(-1.0, -1.0),
        float2( 1.0, -1.0),
        float2(-1.0,  1.0),
        float2( 1.0,  1.0)
    };
    float2 uvs[4] = {
        float2(0.0, 1.0),
        float2(1.0, 1.0),
        float2(0.0, 0.0),
        float2(1.0, 0.0)
    };
    
    out.position = float4(grid[vertex_id], 0.0, 1.0);
    out.uv = uvs[vertex_id];
    return out;
}

fragment float4 fragment_main(VertexOut in [[stage_in]],
                               constant float& time [[buffer(0)]]) {
    float2 uv = in.uv;
    // Time scrolling
    float2 scrolled_uv = uv + float2(time * 0.02, time * 0.01);
    
    // Grid pattern using sin/step
    float2 grid_scale = scrolled_uv * 25.0;
    float2 grid_val = abs(sin(grid_scale * 3.14159265));
    float2 grid_line = step(0.97, grid_val);
    float grid_mask = max(grid_line.x, grid_line.y);
    
    // Pulsing effect
    float pulse = 0.5 + 0.5 * sin(time * 2.5);
    
    // Cyan glow on grid lines
    float4 grid_color = float4(0.0, 0.5, 0.8, 0.2 + 0.1 * pulse) * grid_mask;
    
    // Dark transparent background
    float4 background_color = float4(0.01, 0.01, 0.03, 0.85);
    
    return background_color + grid_color;
}
