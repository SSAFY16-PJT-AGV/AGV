#pragma once

#include "CoreMinimal.h"
#include "GameFramework/HUD.h"
#include "ObjectInteractionHUD.generated.h"

UCLASS()
class AGVSIMULATION_API AObjectInteractionHUD : public AHUD
{
    GENERATED_BODY()

public:
    virtual void DrawHUD() override;
};